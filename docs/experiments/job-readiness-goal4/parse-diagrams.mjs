import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {JSDOM} from 'jsdom';
const window = new JSDOM('<!doctype html><html><body></body></html>').window;
globalThis.window = window; globalThis.document = window.document;
const {default: mermaid} = await import('mermaid');
mermaid.initialize({startOnLoad:false, securityLevel:'strict'});
const root=process.argv[2];
if (!root) throw new Error('Pass the repository root as argument');
const files=['README.md','docs/internship/showcase/architecture.md','docs/internship/showcase/chains.md','docs/internship/interview.md'];
const results=[];
for (const file of files){
 const text=fs.readFileSync(path.join(root,file),'utf8');
 const matches=[...text.matchAll(/```mermaid\n([\s\S]*?)```/g)];
 for(let i=0;i<matches.length;i++){
  const diagram=matches[i][1];
  const result=await mermaid.parse(diagram);
  results.push({file,diagram:i+1,diagram_type:result.diagramType,sha256:crypto.createHash('sha256').update(diagram).digest('hex'),syntax:'passed'});
 }
}
const version=JSON.parse(fs.readFileSync(new URL('node_modules/mermaid/package.json',import.meta.url))).version;
const report={schema_version:1,mermaid_version:version,mode:'syntax parse with jsdom; not a rendered screenshot',diagrams:results,all_passed:true};
fs.writeFileSync(new URL('mermaid-parse.json',import.meta.url),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report));
