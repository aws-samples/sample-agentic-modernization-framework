import { describe, expect, it } from 'vitest';
import { cleanAtxDoc } from '../atxDoc';

// Head of APP-CLI's ATXDocumentation/project-overview.md (2026-10-19).
const PROJECT_OVERVIEW = `# Project Overview

[← Back to index](README.md)

## Contents
- [Purpose](#purpose)
- [Repository layout](#repository-layout)

## Purpose
serverless-java-frameworks-samples is an AWS sample repository.

## Repository layout
See [modules](reference/modules.md).
`;

describe('cleanAtxDoc', () => {
  it('drops the title, back link and contents list, keeping the content', () => {
    const out = cleanAtxDoc(PROJECT_OVERVIEW);
    expect(out.startsWith('## Purpose\n')).toBe(true);
    expect(out).not.toContain('Back to index');
    expect(out).not.toContain('## Contents');
    expect(out).toContain('## Repository layout');
    expect(out).toContain('[modules](reference/modules.md)');
  });

  it('drops a nav line that lists sibling docs after the back link', () => {
    const md = '# Components\n\n[← Back to index](../README.md) · [patterns](patterns.md)\n\n## Component map\nx\n';
    expect(cleanAtxDoc(md)).toBe('## Component map\nx\n');
  });

  it('drops a repeated title but keeps the first real heading', () => {
    const md = '# Technical Debt Report\n# Technical Debt Report\n\n## Recommendation\ny\n';
    expect(cleanAtxDoc(md)).toBe('## Recommendation\ny\n');
  });

  it('leaves a doc without ATX chrome unchanged apart from its title', () => {
    expect(cleanAtxDoc('# Code Metrics\n\n| a | b |\n')).toBe('| a | b |\n');
    expect(cleanAtxDoc('Plain text only')).toBe('Plain text only');
  });
});
