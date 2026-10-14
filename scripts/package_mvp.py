from pathlib import Path
import zipfile
root=Path(__file__).resolve().parents[1]
target=root/'deliverables/SalesCare-mvp.zip';target.parent.mkdir(exist_ok=True)
excluded={'runtime','.mvp-runtime','.mvp-venv','.venv','node_modules','__pycache__','.pytest_cache','deliverables','.git'}
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
    for path in root.rglob('*'):
        relative=path.relative_to(root)
        if not path.is_file() or any(x in excluded for x in relative.parts):continue
        if path.suffix.lower() in {'.pyc','.tsbuildinfo','.dump','.log','.pem','.key'} or path.name.startswith('.env'):continue
        archive.write(path,Path('salescare')/relative)
    names=archive.namelist()
    assert not any('/.mvp-runtime/' in name or '/runtime/' in name or '/.mvp-venv/' in name for name in names)
print('MVP source and compiled frontend packaged; runtime data, credentials and dependencies excluded.')
