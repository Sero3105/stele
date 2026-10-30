// Minimal Markdown -> HTML for previews: headings, paragraphs, lists, tables, images,
// links, code, bold/italic, math blocks. All text is HTML-escaped first; the result is
// shown in a sandboxed iframe without scripts, so document content can't execute.
(function () {
  const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const safeUrl = u => /^(https?:|data:image\/|[^:]*$)/i.test(u) ? u : "#";
  let assetBase = "";  // prefix for relative image paths (no reliance on <base>: Chrome's preloader ignores it in srcdoc)

  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, a, u) =>
      `<img alt="${a}" src="${safeUrl(/^[a-z]+:|^\//i.test(u) ? u : assetBase + u)}">`);
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, t, u) => `<a href="${safeUrl(u)}" target="_blank" rel="noopener">${t}</a>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    return s;
  }

  function table(lines) {
    // Split on unescaped pipes without regex lookbehind (unsupported before Safari 16.4).
    const cells = l => l.trim().replace(/^\||\|$/g, "").replace(/\\\|/g, "\u0000").split("|")
      .map(c => inline(c.trim().replace(/\u0000/g, "|")));
    const [head, , ...body] = lines;
    return "<table><thead><tr>" + cells(head).map(c => `<th>${c}</th>`).join("") + "</tr></thead><tbody>" +
      body.map(r => "<tr>" + cells(r).map(c => `<td>${c}</td>`).join("") + "</tr>").join("") + "</tbody></table>";
  }

  function render(md, base) {
    assetBase = base || "";
    const out = [];
    const fm = md.match(/^---\n([\s\S]*?)\n---\n/);  // YAML front matter -> properties box
    if (fm) {
      md = md.slice(fm[0].length);
      const props = fm[1].split("\n").map(l => l.match(/^(\w+):\s*"?(.*?)"?$/)).filter(Boolean);
      out.push("<dl class=\"props\">" + props.map(p => `<dt>${esc(p[1])}</dt><dd>${esc(p[2].replace(/\\"/g, '"'))}</dd>`).join("") + "</dl>");
    }
    md = md.replace(/<!--[\s\S]*?-->/g, "");
    const lines = md.split("\n");
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) { i++; continue; }
      let m;
      if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); i++; continue; }
      if (l.startsWith("```")) {
        const buf = []; i++;
        while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
        i++; out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`); continue;
      }
      if (l.trim() === "$$" || l.trim().startsWith("$$")) {
        const buf = [l.trim().replace(/^\$\$/, "")]; i++;
        while (i < lines.length && !buf[buf.length - 1].endsWith("$$")) buf.push(lines[i++]);
        out.push(`<pre class="math">${esc(buf.join("\n").replace(/\$\$$/, ""))}</pre>`); continue;
      }
      if (l.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
        const buf = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) buf.push(lines[i++]);
        out.push(table(buf)); continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(l)) {
        const ordered = /^\s*\d/.test(l);
        const buf = [];
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) buf.push(lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        const tag = ordered ? "ol" : "ul";
        out.push(`<${tag}>` + buf.map(b => `<li>${inline(b)}</li>`).join("") + `</${tag}>`); continue;
      }
      if (l.startsWith(">")) {
        const buf = [];
        while (i < lines.length && lines[i].startsWith(">")) buf.push(lines[i++].replace(/^>\s?/, ""));
        out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`); continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\||\$\$)/.test(lines[i].trim())) buf.push(lines[i++]);
      if (!buf.length) buf.push(lines[i++]);
      out.push(`<p>${inline(buf.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  window.renderMarkdown = render;
})();
