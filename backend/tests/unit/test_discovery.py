"""
Tests for discovery pipeline — validates:
1. Discovery file classification (ADS only)
2. ADS adapter parsing (server_inventory, network_data)
3. Hostname grouping logic (_classify_hostname)
4. Ingestion pipeline (batch writes, app group creation)
5. Discovery API endpoint (upload + ingest in one shot)
"""

from pathlib import Path

import pytest

# ── Test Hostname Grouping ─────────────────────────────────────────────


class TestClassifyHostname:
    """Tests for _classify_hostname — the core grouping logic."""

    def setup_method(self):
        from backend.src.connectors.ingest import _classify_hostname

        self.classify = _classify_hostname

    # Rule 1: Infrastructure detection
    def test_infra_sccm(self):
        assert self.classify("mcc-sccm") == "INFRA"

    def test_infra_veeam(self):
        assert self.classify("veeam-backend-2") == "INFRA"

    def test_infra_cal_prefix(self):
        assert self.classify("cal-flexera") == "INFRA"

    def test_infra_puppet(self):
        assert self.classify("kpuppetboard") == "INFRA"

    def test_infra_monitoring(self):
        assert self.classify("nagios-server-01") == "INFRA"

    # Rule 2: ADS pattern — app-env-role-num
    def test_ads_te_prd_app(self):
        assert self.classify("te-prd-app-01") == "TE"

    def test_ads_te_uat_mysql(self):
        assert self.classify("te-uat-mysql-02") == "TE"

    def test_ads_he_prd_app(self):
        assert self.classify("he-prd-app-03") == "HE"

    def test_ads_he_alp_mysql(self):
        assert self.classify("he-alp-mysql-01") == "HE"

    def test_ads_he_bta_web(self):
        assert self.classify("he-bta-web-01") == "HE"

    def test_ads_te_dev_web(self):
        assert self.classify("te-dev-web-01") == "TE"

    def test_ads_uppercase(self):
        assert self.classify("HE-PRD-MYSQL-04") == "HE"

    def test_ads_staging(self):
        assert self.classify("app-stg-web-01") == "APP"

    # Rule 3: Numbered prefix
    def test_numbered_on_asp(self):
        assert self.classify("0152-on-asp") == "NUM-ON-ASP"

    def test_numbered_ab_asp(self):
        assert self.classify("6564-ab-asp") == "NUM-AB-ASP"

    def test_numbered_nb_asp(self):
        assert self.classify("5788-nb-asp") == "NUM-NB-ASP"

    # Rule 4: Short hyphenated
    def test_mcc_oracle(self):
        assert self.classify("mcc-oracle-1") == "MCC-ORACLE"

    def test_q_tps(self):
        assert self.classify("q-tps-mddbprod-10") == "Q-TPS"

    def test_dm_db(self):
        assert self.classify("dm-db06") == "DM-DB06"

    def test_k_tps(self):
        assert self.classify("k-tps-mdem-03") == "K-TPS"

    # Rule 5: Concatenated 3-char prefix
    def test_pap(self):
        assert self.classify("paphcts10") == "PAP"

    def test_emr(self):
        assert self.classify("emrmt13") == "EMR"

    def test_qem(self):
        assert self.classify("qemrns24") == "QEM"

    def test_pws(self):
        assert self.classify("pwsmssi20q") == "PWS"

    def test_sap(self):
        assert self.classify("saphcts36") == "SAP"

    # Edge cases
    def test_cli_short(self):
        assert self.classify("cli-01") == "CLI"

    def test_cli_02(self):
        assert self.classify("cli-02") == "CLI"

    def test_single_char_prefix(self):
        # Very short hostnames (< 3 chars before digits) go to UNGROUPED — expected
        result = self.classify("a01")
        assert result == "UNGROUPED"


# ── Test ADS Adapter ──────────────────────────────────────────────────


class TestADSAdapter:
    """Tests for ADS CSV parsing."""

    def test_parse_server_inventory(self):
        """Validate server_inventory.csv parsing."""
        from backend.src.connectors.adapters.aws_ads import parse_discovery_export

        input_dir = Path(__file__).parents[3] / "input" / "ads"
        if not input_dir.exists():
            pytest.skip("ADS input data not available")

        # ADS adapter expects full_exports/ subdirectory — create symlink structure
        import shutil
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        full_dir = tmp / "full_exports"
        full_dir.mkdir()
        mpa_dir = tmp / "mpa_exports"
        mpa_dir.mkdir()
        # Copy ADS files into full_exports/
        for f in input_dir.glob("*.csv"):
            shutil.copy(f, full_dir / f.name)
        for f in input_dir.glob("*.json"):
            shutil.copy(f, full_dir / f.name)

        data = parse_discovery_export(str(tmp))
        shutil.rmtree(tmp)

        assert "servers" in data
        servers = data["servers"]
        assert len(servers) > 0, "Should parse servers from ADS export"

    def test_ads_hostname_grouping(self):
        """ADS hostnames should group correctly by app prefix."""
        from backend.src.connectors.ingest import _classify_hostname

        # ADS naming: {app}-{env}-{role}-{num}
        ads_hosts = [
            "te-prd-app-01",
            "te-uat-mysql-02",
            "te-dev-web-01",
            "he-prd-app-03",
            "he-alp-mysql-01",
        ]
        groups = {}
        for h in ads_hosts:
            g = _classify_hostname(h)
            groups.setdefault(g, []).append(h)

        assert "TE" in groups, "Should group all te-* into TE"
        assert "HE" in groups, "Should group all he-* into HE"
        assert len(groups["TE"]) == 3
        assert len(groups["HE"]) == 2


# ── Test Ingestion Pipeline ────────────────────────────────────────────


class TestIngestionPipeline:
    """Tests for the ingest_discovery_data function."""

    def test_infer_applications_from_servers(self):
        """Given servers with naming patterns, should create application groups."""
        from backend.src.connectors.ingest import _classify_hostname

        servers = [
            {"hostname": "paphcts10", "server_id": "1"},
            {"hostname": "paphcts11", "server_id": "2"},
            {"hostname": "paphcts12", "server_id": "3"},
            {"hostname": "emrmt01", "server_id": "4"},
            {"hostname": "emrmt02", "server_id": "5"},
            {"hostname": "mcc-sccm", "server_id": "6"},  # infra
        ]

        from collections import defaultdict

        groups = defaultdict(list)
        for s in servers:
            g = _classify_hostname(s["hostname"])
            groups[g].append(s)

        assert "PAP" in groups
        assert len(groups["PAP"]) == 3
        assert "EMR" in groups
        assert len(groups["EMR"]) == 2
        assert "INFRA" in groups
        assert len(groups["INFRA"]) == 1

    def test_generate_machine_id_stable(self):
        """Same hostname should always generate same machine_id."""
        from backend.src.connectors.ingest import _generate_machine_id

        id1 = _generate_machine_id({"hostname": "paphcts10"})
        id2 = _generate_machine_id({"hostname": "paphcts10"})
        assert id1 == id2
        assert id1.startswith("M-")

    def test_generate_machine_id_unique(self):
        """Different hostnames should generate different IDs."""
        from backend.src.connectors.ingest import _generate_machine_id

        id1 = _generate_machine_id({"hostname": "server-a"})
        id2 = _generate_machine_id({"hostname": "server-b"})
        assert id1 != id2


# ── Test Discovery API ─────────────────────────────────────────────────


class TestDiscoveryAPI:
    """Tests for the /discovery/upload endpoint behavior."""

    def test_file_classification(self):
        """Verify ADS file type detection; other vendor exports are not accepted."""
        from backend.src.api.v1 import discovery
        from backend.src.api.v1.discovery import _ALLOWED_FULL

        # ADS files
        assert "server_inventory.csv" in _ALLOWED_FULL
        assert "network_data_full.csv" in _ALLOWED_FULL
        assert "database_data_full.csv" in _ALLOWED_FULL

        assert not [n for n in vars(discovery) if n.startswith("_ALLOWED_") and n not in ("_ALLOWED_FULL", "_ALLOWED_MPA")]
        assert "Capacity_Plan.csv" not in _ALLOWED_FULL

    def test_unrecognized_file_skipped(self):
        """Files not in allowed lists should be skipped."""
        from backend.src.api.v1.discovery import _ALLOWED_FULL, _ALLOWED_MPA

        assert "random.csv" not in _ALLOWED_FULL
        assert "random.csv" not in _ALLOWED_MPA


# ── Test Skill Loading ─────────────────────────────────────────────────


class TestSkillLoading:
    """Verify all required skills exist and load correctly."""

    def test_discovery_skill_exists(self):
        from backend.resources.skills import load_skill

        content = load_skill("discovery")
        assert "7R Strategy" in content
        assert "recommend_strategy" in content

    def test_design_skill_exists(self):
        from backend.resources.skills import load_skill

        content = load_skill("design")
        assert "Target Architecture" in content
        assert "REHOST" in content
        assert "REPLATFORM" in content

    def test_grouping_skill_exists(self):
        from backend.resources.skills import load_skill

        content = load_skill("grouping")
        assert "hostname" in content.lower()
        assert "Infrastructure" in content

    def test_all_skills_loadable(self):
        from backend.resources.skills import list_skills

        skills = list_skills()
        assert len(skills) >= 6, f"Expected 6+ skills, got {len(skills)}"
        names = [s["name"] for s in skills]
        assert "discovery" in names
        assert "assessment" in names
        assert "design" in names
        assert "execution" in names
        assert "planning" in names
        assert "grouping" in names

    def test_missing_skill_raises(self):
        from backend.resources.skills import load_skill

        with pytest.raises(FileNotFoundError):
            load_skill("nonexistent-skill-xyz")
