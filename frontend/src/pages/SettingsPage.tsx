import React, { useState, useEffect, useCallback } from 'react';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Button from '@cloudscape-design/components/button';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Modal from '@cloudscape-design/components/modal';
import Alert from '@cloudscape-design/components/alert';
import Textarea from '@cloudscape-design/components/textarea';
import Tabs from '@cloudscape-design/components/tabs';
import FormField from '@cloudscape-design/components/form-field';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import { listSkills, uploadSkills, getSkill, Skill } from '../api/skills';
import { getInfrastructure, type Infrastructure } from '../api/infrastructure';

export const SettingsPage: React.FC = () => {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [showUpload, setShowUpload] = useState(false);
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState('');
  const [uploadError, setUploadError] = useState('');
  const [viewSkill, setViewSkill] = useState<{ name: string; content: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listSkills();
      setSkills(Array.isArray(data) ? data : []);
    } catch {
      setSkills([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => { load(); }, [load]);

  const [infra, setInfra] = useState<Infrastructure | null>(null);
  useEffect(() => {
    getInfrastructure().then(setInfra).catch(() => setInfra(null));
  }, []);

  const handleUpload = async () => {
    if (!uploadFiles.length) return;
    setUploading(true);
    setUploadError('');
    setUploadStatus('');
    try {
      const resp: any = await uploadSkills(uploadFiles);
      const uploaded = resp?.uploaded || [];
      const skipped = resp?.skipped || [];
      setUploadStatus(
        `Uploaded ${uploaded.length} file(s)${skipped.length ? `, skipped ${skipped.length}` : ''}. Active on next agent run.`
      );
      await load();
      setTimeout(() => { setShowUpload(false); setUploadFiles([]); setUploadStatus(''); }, 2500);
    } catch (e: any) {
      setUploadError(e?.message || 'Upload failed');
    }
    setUploading(false);
  };

  const handleView = async (name: string) => {
    try {
      const resp: any = await getSkill(name);
      setViewSkill({ name, content: resp?.content || '' });
    } catch {
      setViewSkill({ name, content: '(Failed to load)' });
    }
  };

  return (
    <SpaceBetween size="l">
      <Header variant="h1" description="Manage agent skill files">
        Settings
      </Header>

      <Tabs tabs={[
        {
          label: 'Agent Skills',
          id: 'skills',
          content: (
            <SpaceBetween size="m">
              <Container
                header={
                  <Header
                    variant="h2"
                    description="Skills define how agents behave in each workflow phase. Upload .md files to add or update skills. Changes reach the Harness on the next terraform apply (scripts/setup.sh)."
                    actions={
                      <Button variant="primary" onClick={() => { setShowUpload(true); setUploadError(''); setUploadStatus(''); }}>
                        Upload Skills
                      </Button>
                    }
                  >
                    Agent Skills
                  </Header>
                }
              >
                <Table
                  items={skills}
                  loading={loading}
                  columnDefinitions={[
                    {
                      id: 'name',
                      header: 'Skill Name',
                      cell: (item) => (
                        <Button variant="link" onClick={() => handleView(item.name)}>
                          {item.name}
                        </Button>
                      ),
                      width: 150,
                      minWidth: 150,
                    },
                    {
                      id: 'description',
                      header: 'Description',
                      cell: (item) => item.description || '—',
                    },
                    {
                      id: 'source',
                      header: 'Status',
                      cell: () => (
                        <Badge color="green">Active</Badge>
                      ),
                      width: 100,
                    },
                    {
                      id: 'actions',
                      header: 'Actions',
                      cell: (item) => (
                        <SpaceBetween direction="horizontal" size="xs">
                          <Button variant="link" onClick={() => handleView(item.name)}>View</Button>
                        </SpaceBetween>
                      ),
                      width: 80,
                    },
                  ]}
                  empty={<Box textAlign="center"><b>No skills found</b></Box>}
                />
              </Container>

              {/* Skill categories explanation */}
              <Container header={<Header variant="h3">Skill Files (Harness phase playbooks)</Header>}>
                <ColumnLayout columns={2} variant="text-grid">
                  <div>
                    <Box variant="awsui-key-label">Phase Skills</Box>
                    <Box fontSize="body-s">
                      <b>discovery.md</b> / <b>risk_scoring.md</b> — 7R strategy + risk score<br />
                      <b>grouping.md</b> — Server → app grouping<br />
                      <b>assessment.md</b> — Assessment summary<br />
                      <b>planning.md</b> — Migration + wave plans<br />
                      <b>design.md</b> — Design document<br />
                      <b>execution.md</b> / <b>validation.md</b> — Execution + checks<br />
                      <b>handover.md</b> — Handover / close-out
                    </Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">How It Works</Box>
                    <Box fontSize="body-s">
                      All playbooks are composed into the single<br />
                      AgentCore Harness system prompt (harness.tf).<br />
                      <br />
                      Upload → saved locally + staged to S3.<br />
                      Takes effect after <b>terraform apply</b>.
                    </Box>
                  </div>
                </ColumnLayout>
              </Container>
            </SpaceBetween>
          ),
        },
        {
          label: 'Infrastructure',
          id: 'infra',
          content: (
            <Container header={<Header variant="h2">AWS Resources</Header>}>
              {infra ? (
                <ColumnLayout columns={2} variant="text-grid">
                  {([
                    ['Step Functions', infra.workflow.state_machine],
                    ['AgentCore Harness', infra.harness.id ? `${infra.harness.id} (${infra.harness.model})` : ''],
                    ['AgentCore Gateway', infra.gateway.id],
                    ['DynamoDB Table', infra.dynamodb_table],
                    ['S3 Bucket', infra.s3_bucket],
                    ['ECS Cluster', infra.ecs.cluster],
                    ['Graph Backend', `${infra.graph_backend}${infra.wave_planning_enabled ? '' : ' (wave planning disabled)'}`],
                    ['Region', infra.region],
                  ] as const).map(([label, value]) => (
                    <div key={label}>
                      <Box variant="awsui-key-label">{label}</Box>
                      <Box>{value || '—'}</Box>
                    </div>
                  ))}
                </ColumnLayout>
              ) : (
                <Box color="text-status-inactive">Infrastructure details unavailable.</Box>
              )}
            </Container>
          ),
        },
      ]} />

      {/* Upload Modal */}
      <Modal
        visible={showUpload}
        onDismiss={() => !uploading && setShowUpload(false)}
        header="Upload Skill Files"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setShowUpload(false)} disabled={uploading}>Cancel</Button>
              <Button variant="primary" onClick={handleUpload} loading={uploading} disabled={!uploadFiles.length}>
                Upload
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label="Skill Files (.md)"
            description="Upload markdown files. Filename becomes the skill name (e.g., discovery.md → skill 'discovery'). Changes reach the Harness on the next terraform apply (scripts/setup.sh)."
          >
            <input
              type="file"
              multiple
              accept=".md"
              onChange={(e) => setUploadFiles(Array.from(e.target.files || []))}
            />
          </FormField>
          {uploadFiles.length > 0 && (
            <Box fontSize="body-s">
              {uploadFiles.length} file(s): {uploadFiles.map(f => f.name).join(', ')}
            </Box>
          )}
          {uploadStatus && <Alert type="success">{uploadStatus}</Alert>}
          {uploadError && <Alert type="error">{uploadError}</Alert>}
        </SpaceBetween>
      </Modal>

      {/* View Skill Modal */}
      {viewSkill && (
        <Modal
          visible
          onDismiss={() => setViewSkill(null)}
          header={`Skill: ${viewSkill.name}`}
          size="large"
        >
          <Textarea
            value={viewSkill.content}
            readOnly
            rows={30}
          />
        </Modal>
      )}
    </SpaceBetween>
  );
};

export default SettingsPage;
