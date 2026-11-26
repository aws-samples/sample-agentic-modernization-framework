// mermaid is large (~1 MB+ with its diagram chunks) — load it on first use, not in the main bundle.
type Mermaid = typeof import('mermaid').default;

let ready: Promise<Mermaid> | null = null;

export function loadMermaid(): Promise<Mermaid> {
  if (!ready) {
    ready = import('mermaid').then(({ default: m }) => {
      // 'strict' (mermaid's default, pinned): labels are sanitized and click/JS directives are disabled,
      // so the SVG assigned via innerHTML below can't carry script.
      m.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict' });
      return m;
    });
    ready.catch(() => { ready = null; }); // allow a retry after a failed chunk load
  }
  return ready;
}

/** Render `code` into `el`; on a parse/load error show the source as plain text (never as HTML). */
export function renderMermaidInto(el: HTMLElement, code: string, isActive: () => boolean, preStyle: Partial<CSSStyleDeclaration> = {}): void {
  const id = `mermaid-${crypto.randomUUID().slice(0, 8)}`;
  loadMermaid().then((m) => m.render(id, code)).then(({ svg }) => {
    if (isActive()) el.innerHTML = svg;
  }).catch(() => {
    if (!isActive()) return;
    const pre = document.createElement('pre');
    Object.assign(pre.style, { color: '#666' }, preStyle);
    pre.textContent = code;
    el.replaceChildren(pre);
  });
}
