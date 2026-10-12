"""Package a private plugin after its real MCP endpoint has been verified."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
import zipfile

p=argparse.ArgumentParser()
p.add_argument('endpoint',help='Verified live HTTPS Streamable HTTP endpoint')
args=p.parse_args()
url=urlparse(args.endpoint)
if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path!='/mcp':
    p.error('Supply an HTTPS origin followed by /mcp, without credentials, query, or fragment.')
root=Path(__file__).resolve().parents[1]
source=root/'plugin-template'/'truepage-scan'
manifest=json.loads((source/'plugin.json').read_text())
assert len(manifest['extensions']['com.openai']['interface']['shortDescription'])<=30
out=root/'dist';out.mkdir(exist_ok=True)
with zipfile.ZipFile(out/'truepage-scan-plugin.zip','w',zipfile.ZIP_DEFLATED) as z:
    for file in source.rglob('*'):
        if file.is_file():
            z.write(file,Path('truepage-scan')/file.relative_to(source))
    config={'$schema':'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json','mcpServers':{'truepage-scan':{'type':'streamable-http','url':args.endpoint}}}
    z.writestr('truepage-scan/mcp.json',json.dumps(config,indent=2))
print(out/'truepage-scan-plugin.zip')
