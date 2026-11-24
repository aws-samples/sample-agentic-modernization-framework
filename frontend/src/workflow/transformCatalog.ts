// Transform_Catalog_Source (Requirements 8.4, 11.4, 20.3).
// Single authority for the ATX transformation catalog and the default-checked
// (recommended) condition derived from an application's attributes.
//
// The checklist follows the LIVE list (GET /workbench/transformations = `atx custom def list`,
// AWS-managed only): the known items below only add a category, label and pre-check rule.
// Passing `live` drops known items ATX no longer offers and lists new ones under
// "Other AWS transformations"; without it (list unavailable) the known items are shown.

export interface TransformItem {
  id: string;
  label: string;
  lang: string;
  /** Whether this transform is recommended (default-checked) for the app. */
  condition: boolean;
  configLabel?: string;
  configOptions?: string[];
}

export interface TransformCategory {
  category: string;
  items: TransformItem[];
}

/** One entry of GET /workbench/transformations. */
export interface LiveTransformation {
  name: string;
  display_name?: string;
  category?: string;
}

/** Needs scanner findings, and AMF ships no scanner, so it is not offered (backend: workbench skips it). */
const SECURITY_FIXER_TD = 'AWS/security-issue-fixer';

// Live definitions that analyse or advise rather than change code — not migration steps.
const NON_STEP = /analysis|assessment|readiness|advisor|extraction/i;

/** Minimal app shape the catalog inspects. */
export interface CatalogApp {
  language?: string;
  framework?: string;
  runtime?: { jdk_version?: string; app_server?: string };
  repos?: Array<{ url?: string; name?: string }>;
}

function detectLangs(app: CatalogApp) {
  const lang = (app.language || '').toLowerCase();
  const framework = (app.framework || '').toLowerCase();
  const repoNames = (app.repos || [])
    .map((r) => (r.url || r.name || '').toLowerCase())
    .join(' ');
  return {
    lang,
    framework,
    jdk: app.runtime?.jdk_version || '',
    appServer: (app.runtime?.app_server || '').toLowerCase(),
    isJava:
      lang.includes('java') ||
      repoNames.includes('java') ||
      repoNames.includes('spring') ||
      framework.includes('spring'),
    isPython:
      lang.includes('python') ||
      repoNames.includes('python') ||
      repoNames.includes('django') ||
      repoNames.includes('flask'),
    isNode:
      lang.includes('node') ||
      repoNames.includes('node') ||
      repoNames.includes('express') ||
      repoNames.includes('react'),
  };
}

/** Build the full catalog with per-item recommended (default-checked) conditions. */
export function buildTransformCatalog(app: CatalogApp, live?: LiveTransformation[]): TransformCategory[] {
  const known = knownCatalog(app);
  if (!live || live.length === 0) return known;
  const available = new Set(live.map((t) => t.name));
  const knownIds = new Set(known.flatMap((c) => c.items.map((i) => i.id)));
  const cats = known
    .map((c) => ({ ...c, items: c.items.filter((i) => available.has(i.id)) }))
    .filter((c) => c.items.length > 0);
  const other = live
    .filter((t) => !knownIds.has(t.name) && t.name !== SECURITY_FIXER_TD && t.category !== 'analysis' && !NON_STEP.test(t.name))
    .map((t) => ({ id: t.name, label: t.display_name || t.name.replace(/^AWS\//, ''), lang: '', condition: false }));
  return other.length ? [...cats, { category: 'Other AWS transformations', items: other }] : cats;
}

function knownCatalog(app: CatalogApp): TransformCategory[] {
  const { lang, framework, jdk, appServer, isJava, isPython, isNode } = detectLangs(app);

  return [
    {
      category: 'Runtime Upgrades',
      items: [
        { id: 'AWS/java-version-upgrade', label: 'Java Version Upgrade', lang: 'java', condition: isJava && (!jdk || parseInt(jdk) < 17), configLabel: 'Target JDK', configOptions: ['17', '21'] },
        { id: 'AWS/python-version-upgrade', label: 'Python Version Upgrade', lang: 'python', condition: isPython },
        { id: 'AWS/nodejs-version-upgrade', label: 'Node.js Version Upgrade', lang: 'nodejs', condition: isNode },
        { id: 'AWS/oracle-java-to-corretto', label: 'Oracle JDK → Amazon Corretto', lang: 'java', condition: isJava },
        { id: 'AWS/ruby-upgrade', label: 'Ruby Upgrade (2.x → 4.0)', lang: 'ruby', condition: lang.includes('ruby') },
      ],
    },
    {
      category: 'SDK Migrations',
      items: [
        { id: 'AWS/java-aws-sdk-v1-to-v2', label: 'AWS SDK v1 → v2 (Java)', lang: 'java', condition: isJava },
        { id: 'AWS/python-boto2-to-boto3', label: 'boto2 → boto3 (Python)', lang: 'python', condition: isPython },
        { id: 'AWS/nodejs-aws-sdk-v2-to-v3', label: 'AWS SDK v2 → v3 (Node.js)', lang: 'nodejs', condition: isNode },
      ],
    },
    {
      category: 'Framework Upgrades',
      items: [
        { id: 'AWS/spring-boot-version-upgrade', label: 'Spring Boot Upgrade (→ 4.x)', lang: 'java', condition: isJava && framework.includes('spring') },
        { id: 'AWS/JBoss-to-Spring-Boot', label: 'JBoss/WildFly → Spring Boot', lang: 'java', condition: isJava && appServer.includes('jboss') },
        { id: 'AWS/angular-version-upgrade', label: 'Angular Version Upgrade', lang: 'angular', condition: framework.includes('angular') },
        { id: 'AWS/vue.js-version-upgrade', label: 'Vue.js 2 → 3', lang: 'vue', condition: framework.includes('vue') },
      ],
    },
    {
      category: 'Architecture & Optimization',
      items: [
        { id: 'AWS/java-performance-optimization', label: 'Java Performance Optimization', lang: 'java', condition: isJava },
        { id: 'AWS/early-access-java-x86-to-graviton', label: 'Java x86 → Graviton (ARM64)', lang: 'java', condition: isJava },
        { id: 'AWS/oracle-service-bus-to-aws', label: 'Oracle Service Bus → AWS Serverless', lang: 'java', condition: false },
        { id: 'AWS/mulesoft-to-aws-native-java', label: 'MuleSoft → AWS Native (Java)', lang: 'java', condition: false },
        { id: 'AWS/mulesoft-to-aws-native-python', label: 'MuleSoft → AWS Native (Python)', lang: 'python', condition: false },
      ],
    },
    {
      category: 'Observability',
      items: [
        { id: 'AWS/early-access-log4j-to-slf4j-migration', label: 'Log4j → SLF4J/Logback', lang: 'java', condition: isJava },
        { id: 'AWS/datadog-monitors-to-cloudwatch-alarms', label: 'DataDog → CloudWatch Alarms', lang: 'iac', condition: false },
      ],
    },
  ];
}
