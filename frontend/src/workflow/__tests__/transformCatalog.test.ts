import { describe, it, expect } from 'vitest';
import { buildTransformCatalog } from '../transformCatalog';

const javaApp = { language: 'Java', framework: 'Spring Boot', runtime: { jdk_version: '11' } };
const nodeApp = { language: 'Node.js', repos: [{ url: 'https://github.com/x/express-api' }] };

describe('buildTransformCatalog (single source)', () => {
  it('recommends Java transforms for a Java/Spring app', () => {
    const cat = buildTransformCatalog(javaApp);
    const items = cat.flatMap((c) => c.items);
    const byId = Object.fromEntries(items.map((i) => [i.id, i]));
    expect(byId['AWS/java-version-upgrade'].condition).toBe(true); // jdk 11 < 17
    expect(byId['AWS/spring-boot-version-upgrade'].condition).toBe(true);
    expect(byId['AWS/early-access-java-x86-to-graviton'].condition).toBe(true);
    expect(byId['AWS/python-version-upgrade'].condition).toBe(false);
  });

  it('recommends Node transforms for a Node app', () => {
    const byId = Object.fromEntries(
      buildTransformCatalog(nodeApp).flatMap((c) => c.items).map((i) => [i.id, i]),
    );
    expect(byId['AWS/nodejs-version-upgrade'].condition).toBe(true);
    expect(byId['AWS/nodejs-aws-sdk-v2-to-v3'].condition).toBe(true);
    expect(byId['AWS/java-version-upgrade'].condition).toBe(false);
  });
});

describe('live catalog (atx custom def list)', () => {
  const live = [
    { name: 'AWS/java-version-upgrade', display_name: 'Java Version Upgrade' },
    { name: 'AWS/security-issue-fixer', display_name: 'Security Issue Fixer' },
    { name: 'AWS/java-ee-to-quarkus', display_name: 'Java Ee To Quarkus' },
    { name: 'AWS/comprehensive-codebase-analysis', display_name: 'Comprehensive', category: 'analysis' },
    { name: 'AWS/graviton-readiness-assessment', display_name: 'Graviton Readiness' },
  ];

  it('keeps only known items ATX still offers and adds new step-type ones', () => {
    const cat = buildTransformCatalog(javaApp, live);
    const ids = cat.flatMap((c) => c.items.map((i) => i.id));
    expect(ids).toEqual(['AWS/java-version-upgrade', 'AWS/java-ee-to-quarkus']);
    expect(cat[cat.length - 1].category).toBe('Other AWS transformations');
  });

  it('falls back to the known catalog when the live list is unavailable', () => {
    const ids = buildTransformCatalog(javaApp, []).flatMap((c) => c.items.map((i) => i.id));
    expect(ids).toContain('AWS/spring-boot-version-upgrade');
    expect(ids).not.toContain('AWS/security-issue-fixer');
  });
});
