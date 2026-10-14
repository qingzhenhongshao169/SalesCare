"""Provision official EDB binaries locally without changing system services."""
from pathlib import Path
import httpx
import zipfile

root = Path(__file__).resolve().parents[1] / '.mvp-runtime'
root.mkdir(exist_ok=True)
archive = root / 'postgres-binaries.zip'
if not archive.exists():
    with httpx.stream('GET', 'https://sbp.enterprisedb.com/getfile.jsp?fileid=1260609',
                      follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        if 'zip' not in response.headers.get('content-type','') and 'application/octet-stream' not in response.headers.get('content-type',''):
            raise RuntimeError('Official download did not return a binary archive')
        with archive.with_suffix('.part').open('wb') as target:
            for data in response.iter_bytes(1024 * 1024):
                target.write(data)
        archive.with_suffix('.part').replace(archive)
print('Official binary archive downloaded', archive.stat().st_size, flush=True)
with zipfile.ZipFile(archive) as bundle:
    for entry in bundle.infolist():
        target = (root / entry.filename).resolve()
        if not target.is_relative_to(root.resolve()):
            raise RuntimeError('Invalid archive path')
        if entry.filename.startswith(('pgsql/bin/', 'pgsql/lib/', 'pgsql/share/')):
            bundle.extract(entry, root)
print('PostgreSQL binaries extracted', flush=True)
