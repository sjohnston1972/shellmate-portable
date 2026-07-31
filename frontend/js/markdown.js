/**
 * markdown.js — A small Markdown renderer for the built-in documentation.
 *
 * ShellMate has to work with no internet at all, so the manual ships inside
 * the executable and is rendered here. A library would do this better, but
 * the docs are written in this repository against this renderer, so the
 * subset is known rather than guessed: headings, paragraphs, lists, tables,
 * code blocks, inline formatting, links and blockquotes.
 *
 * Everything is escaped before any markup is produced. The documents are
 * ours, but a renderer that trusts its input is one shipped file away from
 * being an injection point, and there is nothing to gain by being careless
 * with it.
 */
(function () {
  'use strict';

  function escapeHtml(text) {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /** Inline formatting, applied to already-escaped text. */
  function inline(text) {
    return text
      // Code first: its contents must not be processed as anything else.
      .replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
      // Only http(s) and in-document links, so a doc cannot smuggle in a
      // javascript: URL.
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|#[^)\s]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function slug(text) {
    return text.toLowerCase().replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '-');
  }

  /**
   * Render Markdown to HTML.
   *
   * @returns {{html: string, headings: Array<{level:number,text:string,id:string}>}}
   *   The headings are returned so the caller can build a contents list
   *   without parsing the output again.
   */
  function render(source) {
    const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
    const out = [];
    const headings = [];

    let inCode = false;
    let listType = null;      // 'ul' | 'ol' | null
    let item = null;          // the list item being accumulated, if any
    let inTable = false;
    let paragraph = [];

    const closeParagraph = () => {
      if (paragraph.length) {
        out.push(`<p>${inline(escapeHtml(paragraph.join(' ')))}</p>`);
        paragraph = [];
      }
    };
    const closeItem = () => {
      if (item) { out.push(`<li>${inline(escapeHtml(item.join(' ')))}</li>`); item = null; }
    };
    const closeList = () => {
      closeItem();
      if (listType) { out.push(`</${listType}>`); listType = null; }
    };
    const closeTable = () => {
      if (inTable) { out.push('</tbody></table></div>'); inTable = false; }
    };
    const closeAll = () => { closeParagraph(); closeList(); closeTable(); };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Fenced code
      const fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        if (inCode) { out.push('</code></pre>'); inCode = false; }
        else {
          closeAll();
          out.push(`<pre class="md-code"><code data-lang="${escapeHtml(fence[1])}">`);
          inCode = true;
        }
        continue;
      }
      if (inCode) { out.push(escapeHtml(line) + '\n'); continue; }

      if (!line.trim()) { closeAll(); continue; }

      // Headings
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        closeAll();
        const level = heading[1].length;
        const text = heading[2].trim();
        const id = slug(text);
        headings.push({ level, text, id });
        out.push(`<h${level} id="${id}">${inline(escapeHtml(text))}</h${level}>`);
        continue;
      }

      // Horizontal rule
      if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
        closeAll();
        out.push('<hr />');
        continue;
      }

      // Tables: a header row followed by a separator row.
      if (line.includes('|') && /^\s*\|?[\s:-]*\|[\s:|-]*$/.test(lines[i + 1] || '')) {
        closeAll();
        const cells = splitRow(line);
        // Wrapped so a wide table scrolls rather than stretching the page.
        out.push('<div class="md-table-wrap"><table><thead><tr>');
        cells.forEach(c => out.push(`<th>${inline(escapeHtml(c))}</th>`));
        out.push('</tr></thead><tbody>');
        inTable = true;
        i++;                                   // skip the separator
        continue;
      }
      if (inTable) {
        if (!line.includes('|')) { closeTable(); }
        else {
          out.push('<tr>');
          splitRow(line).forEach(c => out.push(`<td>${inline(escapeHtml(c))}</td>`));
          out.push('</tr>');
          continue;
        }
      }

      // Blockquote
      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        closeAll();
        out.push(`<blockquote>${inline(escapeHtml(quote[1]))}</blockquote>`);
        continue;
      }

      // Lists
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+\.\s+(.*)$/);
      if (bullet || numbered) {
        closeParagraph();
        const wanted = bullet ? 'ul' : 'ol';
        if (listType !== wanted) { closeList(); out.push(`<${wanted}>`); listType = wanted; }
        closeItem();
        item = [(bullet || numbered)[1]];
        continue;
      }

      // A plain line directly under a list item is that item continuing onto
      // the next line. The manual is hard-wrapped, so this is common; without
      // it the tail of every wrapped bullet falls out into its own paragraph.
      if (item) { item.push(line.trim()); continue; }

      closeList();

      paragraph.push(line.trim());
    }

    if (inCode) out.push('</code></pre>');
    closeAll();

    return { html: out.join('\n'), headings };
  }

  function splitRow(line) {
    return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
  }

  window.shellmateMarkdown = { render };
})();
