---
name: html-page
description: Create a polished, accessible, self-contained HTML page.
tools:
  - FileWriteTool
permissions:
  - filesystem_write
---

# HTML page workflow

1. Confirm the page audience and the requested content.
2. Build a semantic HTML document with responsive CSS and accessible contrast.
3. Keep the page self-contained unless the user asks for external assets.
4. If a read or shell tool is not declared for this Skill, explain the limitation and ask the caller to provide access or update the Skill scope before using it.
5. Report the output path and state clearly if no separate file or browser check was run.
