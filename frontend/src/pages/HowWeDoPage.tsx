import React from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import { renderMermaidInto } from '../lib/mermaid';

const ARCH_DIAGRAM = `flowchart TD
  Eng["Migration Engineer"] --> DASH["Dashboard (CloudFront + S3)"]
  DASH --> API["API (FastAPI)"]
  API --> SFN["Step Functions: Migration Workflow"]
  SFN --> GATE{"7 Governed Gates (Human Approval)"}
  GATE --> SFN
  SFN --> HARNESS["Bedrock AgentCore Harness"]
  HARNESS --> TOOLS["MCP Gateway Tools (Lambda)"]
  TOOLS --> DDB[("DynamoDB")]
  TOOLS --> S3[("S3")]
  SFN --> ECS["ECS Fargate: ATX / AWS Transform"]
  ECS --> PR[("Git Repo / PR")]
  ECS --> DDB
`;

const MermaidBlock: React.FC<{ code: string }> = ({ code }) => {
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!ref.current) return;
    let active = true;
    renderMermaidInto(ref.current, code, () => active, { textAlign: 'left' });
    return () => { active = false; };
  }, [code]);
  return <div ref={ref} style={{ textAlign: 'center', margin: '12px 0' }} />;
};

export const HowWeDoPage: React.FC = () => {
  return (
    <SpaceBetween size="l">
      <Header variant="h1" description="One AgentCore Harness + AWS Step Functions + deterministic gates = governed migration at scale">
        How It Works
      </Header>

      {/* Key Principle */}
      <Container>
        <Box fontSize="heading-m" fontWeight="bold" textAlign="center" padding="s">
          The agent reasons; code enforces correctness; humans approve at every gate.
        </Box>
        <Box textAlign="center" color="text-body-secondary">
          Step Functions = Journey &nbsp;|&nbsp; Harness = Reasoning &nbsp;|&nbsp; Skills = Phase playbooks &nbsp;|&nbsp; Pre-hooks / post-checks = Guardrails
        </Box>
      </Container>

      {/* The Team: AI Teammates & Human Roles */}
      <Container header={<Header variant="h2" description="Agents work autonomously; humans own the decisions at each gate.">The Team — AI Teammates &amp; Human Roles</Header>}>
        <ColumnLayout columns={2}>
          <div>
            <Box fontWeight="bold" padding={{ bottom: 'xs' }}>AI Teammates <Badge color="blue">agents</Badge></Box>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ backgroundColor: '#f8f9fa', borderBottom: '2px solid #e9ebed' }}>
                  <th style={{ padding: '8px 12px', textAlign: 'left' }}>Teammate</th>
                  <th style={{ padding: '8px 12px', textAlign: 'left' }}>Role on the team</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { name: 'Discovery playbook', role: 'Links infrastructure, recommends the 7R strategy' },
                  { name: 'Assessment playbook', role: 'Reads the ATX code analysis, sizes the target, writes the assessment summary' },
                  { name: 'Design playbook', role: 'Authors the design doc — the single source of truth' },
                  { name: 'Planning playbook', role: 'Builds migration and wave plans' },
                  { name: 'Execution playbook', role: 'Plans DB migration; infra + CI/CD are generated deterministically' },
                  { name: 'Handover playbook', role: 'Writes the handover / close-out document' },
                ].map(a => (
                  <tr key={a.name} style={{ borderBottom: '1px solid #e9ebed' }}>
                    <td style={{ padding: '8px 12px', fontWeight: 600 }}>{a.name}</td>
                    <td style={{ padding: '8px 12px', color: '#5f6b7a' }}>{a.role}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <Box fontWeight="bold" padding={{ bottom: 'xs' }}>Human Roles <Badge color="grey">people</Badge></Box>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr style={{ backgroundColor: '#f8f9fa', borderBottom: '2px solid #e9ebed' }}>
                  <th style={{ padding: '8px 12px', textAlign: 'left' }}>Role</th>
                  <th style={{ padding: '8px 12px', textAlign: 'left' }}>Responsibility</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { role: 'Migration Engineer / Lead', resp: 'Drives the workflow; approves the Discovery, Assessment and Design gates' },
                  { role: 'Application Owner', resp: 'Provides business context; final Go/No-Go sign-off' },
                  { role: 'Security / Compliance Reviewer', resp: 'Reviews the approval audit on the Governance page' },
                  { role: 'Platform / Cloud Engineer', resp: 'Supplies target-env inputs (account, VPC, compute, DB)' },
                ].map(h => (
                  <tr key={h.role} style={{ borderBottom: '1px solid #e9ebed' }}>
                    <td style={{ padding: '8px 12px', fontWeight: 600 }}>{h.role}</td>
                    <td style={{ padding: '8px 12px', color: '#5f6b7a' }}>{h.resp}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </ColumnLayout>
      </Container>

      {/* Process Overview */}
      <Container header={<Header variant="h2">End-to-End Process</Header>}>
        <SpaceBetween size="m">
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ backgroundColor: '#f8f9fa', borderBottom: '2px solid #e9ebed' }}>
                <th style={{ padding: '10px 12px', textAlign: 'left', width: 40 }}>Step</th>
                <th style={{ padding: '10px 12px', textAlign: 'left' }}>Phase</th>
                <th style={{ padding: '10px 12px', textAlign: 'left' }}>What Happens</th>
                <th style={{ padding: '10px 12px', textAlign: 'left' }}>Who</th>
                <th style={{ padding: '10px 12px', textAlign: 'left' }}>Output</th>
              </tr>
            </thead>
            <tbody>
              {[
                { step: '1', phase: 'L1 Discovery', what: 'Import infrastructure data from AWS Application Discovery Service (ADS). Machine inventory with CPU, RAM, storage, network connections.', who: 'Migration Engineer', output: 'Machines in DynamoDB' },
                { step: '2', phase: 'L2 Discovery', what: 'Upload application enrichment data (app-data.json) with business metadata: owners, repos, language, framework, architecture, cost, dependencies.', who: 'Migration Engineer', output: 'Enriched apps in DynamoDB' },
                { step: '3', phase: 'Start Workflow', what: 'Select applications on Workflow page and click Start. Step Functions execution begins.', who: 'Migration Engineer', output: 'SFN execution running' },
                { step: '4', phase: 'Discovery (Agent)', what: 'Pre-hook checks the app, name and linked repo. The Harness links machines and recommends a 7R strategy; a deterministic risk score (0-100) is computed.', who: 'AI Agent', output: 'Strategy + risk score in DynamoDB' },
                { step: '5', phase: 'Discovery Approval', what: 'Engineer reviews the recommendation and confidence. Can override the strategy, approve, or retry with a comment. Retire / retain / repurchase go straight to close-out handover.', who: 'Migration Engineer', output: 'Confirmed strategy' },
                { step: '6', phase: 'Assessment (Agent)', what: 'AWS Transform code analysis on ECS Fargate, then the Harness writes the assessment summary. A post-check rejects one written for the wrong strategy.', who: 'AI Agent', output: 'Reports in S3 + DynamoDB' },
                { step: '7', phase: 'Assessment Approval', what: 'Engineer confirms compute, database, CI/CD target, AWS account, VPC CIDR and the ATX transformations.', who: 'Migration Engineer', output: 'Approved targets' },
                { step: '8', phase: 'Design (Agent)', what: 'Generates the design document. A post-check compares it with the approved targets and re-runs once on contradictions.', who: 'AI Agent', output: 'Design doc in S3' },
                { step: '9', phase: 'Design Approval', what: 'Engineer reviews the design doc and the computed decision summary (changes, risk, warnings).', who: 'Migration Engineer', output: 'Approved design' },
                { step: '10', phase: 'Execution', what: 'Sequential: code transformation (skipped for rehost/relocate) → DB migration plan → Terraform generated + validated + planned (never applied) → CI/CD generated. Committed to a migration branch.', who: 'AI Agent + Workflow', output: 'Branch with IaC + pipeline' },
                { step: '11', phase: 'Validation', what: 'Deterministic deployment check: IaC, CI/CD gates, transform outcome, live pipeline / ECS / ALB state.', who: 'Workflow', output: 'Validation verdict' },
                { step: '12', phase: 'Cutover & Handover', what: 'Cutover plan → Go/No-Go gate → handover documentation.', who: 'AI Agent + Engineer', output: 'App migrated ✓' },
              ].map(row => (
                <tr key={row.step} style={{ borderBottom: '1px solid #e9ebed' }}>
                  <td style={{ padding: '8px 12px', fontWeight: 700, color: '#0972d3' }}>{row.step}</td>
                  <td style={{ padding: '8px 12px', fontWeight: 600 }}>{row.phase}</td>
                  <td style={{ padding: '8px 12px', color: '#5f6b7a' }}>{row.what}</td>
                  <td style={{ padding: '8px 12px' }}><Badge color={row.who.includes('AI') ? 'blue' : 'grey'}>{row.who}</Badge></td>
                  <td style={{ padding: '8px 12px', fontSize: 12 }}>{row.output}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SpaceBetween>
      </Container>

      {/* Workflow Columns */}
      <Container header={<Header variant="h2">Workflow UI Columns</Header>}>
        <Box color="text-body-secondary" fontSize="body-s" padding={{ bottom: 's' }}>
          The workflow page shows 4 columns. Each maps to multiple ASL phases in Step Functions.
        </Box>
        <ColumnLayout columns={4}>
          {[
            { col: 'Discovery', phases: ['discovery'], desc: 'Agent enriches app, links machines, recommends strategy. Approval gate with strategy dropdown.' },
            { col: 'Assessment', phases: ['assessment'], desc: 'ATX code analysis + modernization scoring. Design v1 generated. Approval gate.' },
            { col: 'Design', phases: ['design'], desc: 'Migration plan + design doc v2. Shows version (v1, v2, v3). Required Inputs tab for user fields. Must fill all to approve.' },
            { col: 'Execution', phases: ['transform', 'execution', 'validation', 'deploy', 'post_deploy', 'handover'], desc: 'Code transform, infra, DB, CI/CD, validation, deploy, handover — all automated from design doc.' },
          ].map(c => (
            <div key={c.col} style={{ padding: 12, border: '1px solid #e9ebed', borderRadius: 6 }}>
              <Box fontWeight="bold">{c.col}</Box>
              <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 'xxs' }}>{c.desc}</Box>
              <Box fontSize="body-s" padding={{ top: 'xs' }}>ASL: {c.phases.join(', ')}</Box>
            </div>
          ))}
        </ColumnLayout>
      </Container>


      {/* Agent Architecture */}
      <Container header={<Header variant="h2">Agent Architecture</Header>}>
        <SpaceBetween size="m">
          <Box>
            Step Functions invokes the <strong>Amazon Bedrock AgentCore Harness</strong> for each phase. The Harness reasons and calls
            purpose-built <strong>MCP Gateway tools</strong> (Lambda-backed) to read/write portfolio state, recommend strategy, plan, and
            store documents. Long-running ATX code analysis/transformation runs on <strong>ECS Fargate</strong> and calls back to the workflow.
          </Box>
          <MermaidBlock code={ARCH_DIAGRAM} />
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ backgroundColor: '#f8f9fa', borderBottom: '2px solid #e9ebed' }}>
                <th style={{ padding: '10px 12px', textAlign: 'left' }}>Gateway target</th>
                <th style={{ padding: '10px 12px', textAlign: 'left' }}>Tools</th>
              </tr>
            </thead>
            <tbody>
              {[
                { name: 'portfolio', tools: 'get_app_context, update_app_field, search_machines, assign_machines_to_app, get_portfolio_summary, add_owner' },
                { name: 'strategy', tools: 'recommend_strategy, calculate_completeness' },
                { name: 'planning', tools: 'generate_wave_plan, store_wave_plan, get_wave_plan, store_migration_plan' },
                { name: 'documents', tools: 'store_design_document, get_design_document, store_analysis_result, compute_risk_score, get_analysis_result' },
                { name: 'execution', tools: 'start_code_analysis, start_code_transformation, get_job_status, provision_infrastructure, generate_cicd, commit_cicd_to_repo' },
              ].map(a => (
                <tr key={a.name} style={{ borderBottom: '1px solid #e9ebed' }}>
                  <td style={{ padding: '10px 12px', fontWeight: 600 }}>{a.name}</td>
                  <td style={{ padding: '10px 12px', color: '#5f6b7a', fontFamily: 'monospace', fontSize: 12 }}>{a.tools}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SpaceBetween>
      </Container>

      {/* Approval Gates */}
      <Container header={<Header variant="h2">Approval Gates & Actions</Header>}>
        <Box color="text-body-secondary" fontSize="body-s" padding={{ bottom: 's' }}>
          Every approval gate has 3 actions: <Badge color="blue">Approve</Badge> <Badge color="grey">Cancel Workflow</Badge> <Badge color="grey">Retry</Badge>
        </Box>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ backgroundColor: '#f8f9fa', borderBottom: '2px solid #e9ebed' }}>
              <th style={{ padding: '10px 12px', textAlign: 'left' }}>Gate</th>
              <th style={{ padding: '10px 12px', textAlign: 'left' }}>Approve</th>
              <th style={{ padding: '10px 12px', textAlign: 'left' }}>Retry</th>
              <th style={{ padding: '10px 12px', textAlign: 'left' }}>Cancel</th>
            </tr>
          </thead>
          <tbody>
            {[
              { gate: 'Discovery Review', approve: 'Confirm strategy → proceed to assessment', retry: 'Agent re-runs with additional input', cancel: 'Stop execution' },
              { gate: 'Assessment Review', approve: 'Accept reports → proceed to design', retry: 'Re-run assessment with different repos/strategy', cancel: 'Stop execution' },
              { gate: 'Design Review', approve: 'Only when ALL Required Inputs filled → proceed to execution', retry: 'Agent regenerates doc with user inputs', cancel: 'Stop execution' },
              { gate: 'Execution Review', approve: 'Accept generated IaC + pipeline → proceed to validation', retry: 'Not available (reject → run fails)', cancel: 'Stop execution' },
              { gate: 'Validation Review', approve: 'Accept the validation verdict → proceed to cutover', retry: 'Not available (reject → run fails)', cancel: 'Stop execution' },
              { gate: 'Cutover (Go/No-Go)', approve: 'Proceed to handover', retry: 'Not available', cancel: 'Not ready — stop' },
              { gate: 'Handover / Close-out', approve: 'Complete the run', retry: 'Regenerate the close-out document', cancel: 'Stop execution' },
            ].map(g => (
              <tr key={g.gate} style={{ borderBottom: '1px solid #e9ebed' }}>
                <td style={{ padding: '8px 12px', fontWeight: 600 }}>{g.gate}</td>
                <td style={{ padding: '8px 12px', color: '#2ea597' }}>{g.approve}</td>
                <td style={{ padding: '8px 12px', color: '#0972d3' }}>{g.retry}</td>
                <td style={{ padding: '8px 12px', color: '#d91515' }}>{g.cancel}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Container>

      {/* Data Sources */}
      <Container header={<Header variant="h2">Data Sources & Storage</Header>}>
        <ColumnLayout columns={2}>
          <Container header={<Header variant="h3">Input Data</Header>}>
            <SpaceBetween size="xs">
              <div><strong>L1 Discovery (ADS)</strong> — server_inventory.csv, performance, network, databases (77-137 servers)</div>
              <div><strong>L2 Discovery (app-data.json)</strong> — 30 applications with business metadata, repos, owners, cost, constraints</div>
              <div><strong>Source Code</strong> — AWS CodeCommit repos linked to apps (ATX analyzes these)</div>
            </SpaceBetween>
          </Container>
          <Container header={<Header variant="h3">Storage</Header>}>
            <SpaceBetween size="xs">
              <div><strong>DynamoDB</strong> — Single-table: apps (META), machines, workflows, tokens, assessments, orchestration logs</div>
              <div><strong>S3</strong> — applications/{'{app_id}'}/discovery/, analysis/, design/, transform/</div>
              <div><strong>CloudWatch</strong> — ECS task logs (viewable inline in Workflow UI)</div>
              <div><strong>Step Functions</strong> — Execution history, task ARNs, phase transitions</div>
            </SpaceBetween>
          </Container>
        </ColumnLayout>
      </Container>

      {/* AWS Services */}
      <Container header={<Header variant="h2">AWS Services Used</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div><Box variant="awsui-key-label">Step Functions</Box><Box fontSize="body-s">Workflow orchestration, approval gates (waitForTaskToken), rejection loops</Box></div>
          <div><Box variant="awsui-key-label">ECS Fargate</Box><Box fontSize="body-s">ATX code analysis + transformation tasks (4 vCPU / 8 GB, single Docker image)</Box></div>
          <div><Box variant="awsui-key-label">Bedrock (Claude Sonnet)</Box><Box fontSize="body-s">LLM inference for all agents — strategy, design docs, migration plans</Box></div>
          <div><Box variant="awsui-key-label">DynamoDB</Box><Box fontSize="body-s">Single-table design: PK=APP#{'{id}'} / MACHINE#{'{id}'}, SK=META/WORKFLOW/ASSESSMENT</Box></div>
          <div><Box variant="awsui-key-label">S3</Box><Box fontSize="body-s">ATX reports, design docs (versioned), discovery summaries, transform artifacts</Box></div>
          <div><Box variant="awsui-key-label">Lambda</Box><Box fontSize="body-s">Task token storage (waitForTaskToken → DynamoDB, &lt;50ms)</Box></div>
          <div><Box variant="awsui-key-label">CloudWatch</Box><Box fontSize="body-s">ECS logs (inline viewer in UI), metrics, alarms</Box></div>
          <div><Box variant="awsui-key-label">ECR</Box><Box fontSize="body-s">Container registry for worker image</Box></div>
          <div><Box variant="awsui-key-label">Terraform</Box><Box fontSize="body-s">Infrastructure as code for all resources (infra/environments/dev/)</Box></div>
        </ColumnLayout>
      </Container>

      {/* Skills System */}
      <Container header={<Header variant="h2">Skills System</Header>}>
        <SpaceBetween size="m">
          <Box>
            The Harness's behavior per phase is defined by a <strong>skill playbook</strong> in <code>backend/resources/skills/</code>.
            Terraform composes the playbooks into the Harness system prompt, so a change takes effect after <code>terraform apply</code>.
            Anything correctness-critical (strategy gating, infra + CI/CD generation, validation) is done in code, not the prompt.
          </Box>
          <SpaceBetween size="xxs">
            <div>• <strong>discovery.md</strong> + <strong>risk_scoring.md</strong> — 7R strategy recommendation and risk score</div>
            <div>• <strong>grouping.md</strong> — Server → application grouping</div>
            <div>• <strong>assessment.md</strong> — Assessment summary, sizing, cost projection</div>
            <div>• <strong>planning.md</strong> — Migration and wave plans</div>
            <div>• <strong>design.md</strong> — Design document structure</div>
            <div>• <strong>execution.md</strong> / <strong>validation.md</strong> — DB migration plan, deployment checks</div>
            <div>• <strong>handover.md</strong> — Handover and close-out documents</div>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {/* Design Document */}
      <Container header={<Header variant="h2">Design Document — Single Source of Truth</Header>}>
        <SpaceBetween size="s">
          <Box>
            The design document is the <strong>single source of truth for execution</strong>. Each Design run stores a new version:
          </Box>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ backgroundColor: '#f8f9fa', borderBottom: '2px solid #e9ebed' }}>
                <th style={{ padding: '8px 12px', textAlign: 'left' }}>Version</th>
                <th style={{ padding: '8px 12px', textAlign: 'left' }}>Generated After</th>
                <th style={{ padding: '8px 12px', textAlign: 'left' }}>Contains</th>
              </tr>
            </thead>
            <tbody>
              <tr style={{ borderBottom: '1px solid #e9ebed' }}><td style={{ padding: '8px 12px', fontWeight: 600 }}>v1</td><td style={{ padding: '8px 12px' }}>Assessment</td><td style={{ padding: '8px 12px', color: '#5f6b7a' }}>Current state, assessment summary, initial target architecture</td></tr>
              <tr style={{ borderBottom: '1px solid #e9ebed' }}><td style={{ padding: '8px 12px', fontWeight: 600 }}>v2</td><td style={{ padding: '8px 12px' }}>Design Document</td><td style={{ padding: '8px 12px', color: '#5f6b7a' }}>Full design with target architecture, transformation steps, timeline, cost analysis, target architecture details</td></tr>
              <tr style={{ borderBottom: '1px solid #e9ebed' }}><td style={{ padding: '8px 12px', fontWeight: 600 }}>v3+</td><td style={{ padding: '8px 12px' }}>User Inputs / Retry</td><td style={{ padding: '8px 12px', color: '#5f6b7a' }}>User-provided values (target account, VPC, compute, port, health check), actual resource ARNs post-execution</td></tr>
            </tbody>
          </table>
          <Box fontSize="body-s" color="text-body-secondary">
            12 sections: Executive Summary, Current State, Target Architecture, Migration Approach, Testing, Operations, Timeline, Risks, Cost, Code Transformations, CI/CD, Deployment Checklist.
          </Box>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
};

export default HowWeDoPage;
