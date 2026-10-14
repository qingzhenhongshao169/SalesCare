import json
import os
from pathlib import Path
from server.model_gateway import protect

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.mvp-runtime'


def settings():
    path = Path(os.environ.get('SALESCARE_MVP_CONFIG', RUNTIME / 'config.json'))
    config = json.loads(path.read_text('utf-8')) if path.exists() else {}
    for key in ('database_url', 'admin_database_url', 'setup_token'):
        if key + '_sealed' in config:
            config[key] = protect(config.pop(key + '_sealed'), decrypt=True)
    config['database_url'] = os.environ.get('SALESCARE_DATABASE_URL', config.get('database_url', ''))
    config['admin_database_url'] = os.environ.get('SALESCARE_ADMIN_DATABASE_URL', config.get('admin_database_url',''))
    config['public_origin'] = os.environ.get('SALESCARE_PUBLIC_ORIGIN', config.get('public_origin',''))
    config['admin_username'] = os.environ.get('SALESCARE_INITIAL_USERNAME',config.get('admin_username','admin1'))
    config['admin_name'] = os.environ.get('SALESCARE_INITIAL_NAME',config.get('admin_name','管理员1'))
    config['setup_token'] = os.environ.get('SALESCARE_SETUP_TOKEN',config.get('setup_token',''))
    config['mode'] = os.environ.get('SALESCARE_MODE', config.get('mode', 'production'))
    config['secure_cookies'] = config['mode'] != 'lan-pilot'
    if not config['database_url'].startswith('postgresql'):
        raise RuntimeError('MVP requires PostgreSQL; run the provisioning script first.')
    if config['mode'] not in ('production', 'lan-pilot', 'test'):
        raise RuntimeError('Unsupported MVP deployment mode')
    if config['mode'] == 'production' and not config.get('public_origin','').startswith('https://'):
        raise RuntimeError('Production requires a configured HTTPS origin')
    if config['mode']=='production' and not config['setup_token']:
        raise RuntimeError('Production requires a protected initial setup token')
    return config
