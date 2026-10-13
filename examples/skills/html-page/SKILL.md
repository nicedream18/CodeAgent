---
name: html-page
description: Create a polished, accessible, self-contained HTML page.
tools:
  - FileReadTool
  - FileWriteTool
  - FileEditTool
  - BashTool
permissions:
  - filesystem_read
  - filesystem_write
  - shell
---

# HTML page workflow

1. Confirm the page audience and the requested content.
2. Build a semantic HTML document with responsive CSS and accessible contrast.
3. Keep the page self-contained unless the user asks for external assets.
4. Run a non-interactive check that verifies the file exists and key sections are present.
5. Report the output path and the checks that passed.
