import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { SandboxedHtmlFrame } from '../SandboxedHtmlFrame';

const SRC = join(__dirname, '..', '..');

const sourceFiles = (dir: string): string[] => readdirSync(dir).flatMap((name) => {
  const p = join(dir, name);
  if (statSync(p).isDirectory()) return name === '__tests__' ? [] : sourceFiles(p);
  return /\.(tsx?|jsx?)$/.test(name) ? [p] : [];
});

describe('SandboxedHtmlFrame (H6)', () => {
  it('renders the report in an iframe with an empty sandbox and no referrer', () => {
    const html = renderToStaticMarkup(
      <SandboxedHtmlFrame html={'<div><script>parent.sessionStorage.getItem("id_token")</script></div>'} />,
    );
    expect(html).toMatch(/<iframe[^>]*\ssandbox=""/);
    expect(html).toMatch(/\sreferrerpolicy="no-referrer"/i);
    expect(html).not.toMatch(/allow-scripts|allow-same-origin/);
    // The payload is only ever an attribute value (escaped), never live markup in the page.
    expect(html).not.toContain('<script>');
  });
});

describe('HTML sinks in frontend/src', () => {
  it('every <iframe> is the sandboxed component', () => {
    const offenders = sourceFiles(SRC)
      .filter((f) => !f.endsWith('SandboxedHtmlFrame.tsx'))
      .filter((f) => /<iframe\b|srcDoc=/.test(readFileSync(f, 'utf8')))
      .map((f) => relative(SRC, f));
    expect(offenders).toEqual([]);
  });

  it('nothing uses dangerouslySetInnerHTML, and innerHTML only takes mermaid strict-mode SVG', () => {
    const files = sourceFiles(SRC);
    expect(files.filter((f) => readFileSync(f, 'utf8').includes('dangerouslySetInnerHTML'))).toEqual([]);
    const innerHtml = files.filter((f) => /\.innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=|document\.write/.test(readFileSync(f, 'utf8')));
    expect(innerHtml.map((f) => relative(SRC, f))).toEqual([join('lib', 'mermaid.ts')]);
    expect(readFileSync(join(SRC, 'lib', 'mermaid.ts'), 'utf8')).toContain("securityLevel: 'strict'");
  });
});
