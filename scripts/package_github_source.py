"""Prepare a reviewed, independent collaboration copy without live runtime data."""
import hashlib,json,re,sys,zipfile
from pathlib import Path

SOURCE=Path(__file__).resolve().parents[1]
WORKSPACE=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else SOURCE.parent.parent/'智能客服协作'
PROJECT=WORKSPACE/'SalesCare'
if PROJECT.exists():raise SystemExit('Collaboration copy already exists; retained. Choose another output directory.')
if SOURCE.resolve() in WORKSPACE.parents or WORKSPACE==SOURCE.resolve():raise SystemExit('Output must be outside the running project.')
EXCLUDED={'.git','.mvp-runtime','.mvp-venv','runtime','.venv','node_modules','__pycache__','.pytest_cache','deliverables','dist','mvp-dist','docs'}
TEXT_EXTENSIONS={'.py','.tsx','.ts','.json','.css','.html','.js','.mjs','.cjs','.ps1','.cmd','.txt','.ini','.md','.mako'}
ROOT_FILES={'requirements.txt','mvp-requirements.txt','mvp-requirements.lock.txt','start-mvp.cmd','restart-mvp.cmd','start.cmd','start-lan.cmd','start.ps1','configure-mvp-firewall.ps1','configure-lan-firewall.ps1'}
COPIED_DIRS={'mvp','server','tests','scripts','web'}

def copy(relative):
    path=SOURCE/relative
    if path.is_symlink():raise RuntimeError('Symbolic links require explicit review.')
    dest=PROJECT/relative;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(path.read_bytes())

for path in SOURCE.rglob('*'):
    relative=path.relative_to(SOURCE)
    if not path.is_file() or any(p in EXCLUDED for p in relative.parts):continue
    if path.suffix.lower() not in TEXT_EXTENSIONS or path.name.startswith('.env'):continue
    if relative.parts[0] in COPIED_DIRS or str(relative) in ROOT_FILES:copy(relative)
# Only public seed material, not trial runtime IDs, chat traces, operation guides or screenshots.
for name in ('biaofun.json','biaofun-知识库.md','source-register.json'):copy(Path('knowledge')/name)
for path in (SOURCE/'knowledge/hisense-trial-20261013/知识文档').glob('*.md'):copy(path.relative_to(SOURCE))
copy(Path('knowledge/hisense-trial-20261013/manifest.json'))

def write(relative,text):
    p=PROJECT/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text.strip()+'\n','utf-8')

# One verification script had a developer-machine package path. Make the copy portable.
p=PROJECT/'scripts/verify_document_ui.cjs'
if p.exists():
    s=p.read_text('utf-8');s=s.replace("require('C:/Users/16022/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')","require(process.env.SALESCARE_PLAYWRIGHT_PATH||'playwright')")
    p.write_text(s,'utf-8')

write('.gitignore','''
# Runtime data and credentials never belong in the source repository.
.mvp-runtime/
runtime/
.env
.env.*
!.env.example
*.key
*.pem
*.p12
*.pfx
*.dump
*.sqlite
*.sqlite3
*.db
*.log
backups/
uploads/
# Dependencies, generated artifacts and local editor files.
.mvp-venv/
.venv/
venv/
**/node_modules/
web/dist/
web/mvp-dist/
mvp-dist/
deliverables/
__pycache__/
*.pyc
.pytest_cache/
*.tsbuildinfo
.idea/
.vscode/
Thumbs.db
.DS_Store
''')
write('.gitattributes','''
* text=auto
*.py text eol=lf
*.tsx text eol=lf
*.ts text eol=lf
*.json text eol=lf
*.md text eol=lf
*.cmd text eol=crlf
*.ps1 text eol=crlf
''')
write('README.md','''
# SalesCare 企业智能客服

这是供共同开发的源码仓库，包含客户咨询端、员工后台、企业隔离、知识构建、模型问答与人工接待流程。基于现有内网MVP整理，尚未完成公网及真实外部平台的商业验收。

## 技术栈

- 前端：React、TypeScript、Vite。
- 后端：Python、FastAPI、SQLAlchemy、PostgreSQL、Alembic。
- 知识：原生文件解析、关键词检索、本地/API Embedding、融合排序和重排。
- 独立进程：API、AI/SLA、渠道服务、知识处理。

## 项目目录

| 目录 | 用途 |
| --- | --- |
| web/mvp | 当前MVP前端 |
| mvp | SaaS接口、权限、业务、数据模型与迁移 |
| server | 共用模型网关、回答规则与检索，以及保留的早期Demo |
| scripts | 初始化、启动、备份、模型准备、评测与维护工具 |
| knowledge | 官方公开资料整理样例，无运行中的企业数据库 |
| tests、mvp/tests | 共用模块与MVP检查 |
| docs | 新环境设置、协作规范和接口边界 |

## 先阅读

1. [新环境设置](docs/新环境设置.md)。
2. [两人协作规范](docs/两人协作规范.md)。
3. [数据处理对接草案](docs/数据处理对接草案.md)。

源码包不包含数据库、真实账号密码、API密钥、运行日志、上传原件、本地模型权重、虚拟环境或node_modules。源代码内的synthetic/fake/test凭据仅用于合成测试，不能用作真实登录账号。

新电脑不能直接运行启动脚本，需要先准备独立开发数据库和本机配置。代码提交与数据库迁移、模型资源安装和服务发布是分别进行的工作。

## 状态与边界

已支持常见文字资料标准化、预览、试问、范围调整、审核发布、新版本、撤回和删除；精确型号约束与真实引用已接入回答流程。扫描件OCR、完整订单对象权限、实时交易接口、大规模向量索引仍需进一步建设。

公开资料只是试验样例，部分规则摘要是历史页面信息，不能直接当成当前交易承诺。不要把本仓库当成海信官方客服或海信授权产品。

模型密钥在每家企业后台独立配置；网站SDK和其他平台渠道使用各企业自己的配置。上传代码不会同步原电脑的密钥、账号或业务数据。
''')
write('docs/新环境设置.md',r'''
# 新环境设置

本源码来自Windows内网MVP，主要开发环境为Python 3.13与Node.js 24。新机安装与已有机器的数据迁移不同。

## 安装依赖与构建

在项目根目录使用PowerShell执行：

```powershell
python -m venv .mvp-venv
.\.mvp-venv\Scripts\python.exe -m pip install -r mvp-requirements.lock.txt
npm.cmd ci --prefix web
npm.cmd run build:mvp --prefix web
```

## 选择数据库

先由两人约定专用开发数据库。远程虚拟机的IP能连通，不代表现有表、角色权限与本项目兼容。
本项目依赖PostgreSQL、租户RLS和Alembic迁移；应用账号不得使用绕过RLS的超级用户。
没有确认目标库、备份与表结构前，不要对同事现有业务数据库运行初始化或迁移，也不要把本机配置照抄到同事电脑。

使用一台没有现有SalesCare实例的新Windows机器进行本地开发时，可参考已有工具：

```powershell
.\.mvp-venv\Scripts\python.exe scripts/download_mvp_postgres.py
.\.mvp-venv\Scripts\python.exe scripts/provision_mvp.py
```

这些工具使用固定的本地SalesCare数据目录和数据库端口，仅适合确认过的新实例，不适合在当前运行机器上直接再执行一遍。
初始化脚本中的public_origin须改为该开发环境的实际地址；后台初始化密码由本人设置。
配置保存在被Git忽略的.mvp-runtime目录，需使用本机受保护的配置机制；不把现有Windows密文直接搬到另一台机器。

## 本地知识模型

```powershell
.\.mvp-venv\Scripts\python.exe scripts/prepare_kb_models.py
```

模型权重另行下载，本仓库不提供。本地Embedding与重排独立于聊天模型，聊天模型在企业后台填写密钥后启用。

## 启动与检查

准备好数据库、本机配置与编译产物后运行start-mvp.cmd。默认MVP端口8767；本地开发服务只供约定的测试范围访问。
检查/api/health中的API与三个Worker状态，再通过本机/setup或/platform完成企业开通。
MVP测试会创建独立测试库，需要测试配置与数据库权限。无配置时执行测试失败不代表源码缺失；不要用真实业务库充当测试库。

原项目的UI验收脚本可能要求Chrome和Playwright，可在个人开发环境单独安装并配置。此源码包仅完成静态完整性与前端构建验证，不声称在新机器完成数据库及所有渠道联调。
''')
write('docs/两人协作规范.md','''
# 两人协作规范

先建立一个公司认可的私有Git仓库。当前协作副本独立于财务项目；运行中的原项目继续作为备份和参考。

1. 两人都从同一个仓库获取代码，各自使用工作分支，例如codex/knowledge-ingestion与codex/reception-workflow。
2. 每次提交围绕一个完整任务，写清功能、接口、验证和限制；通过Pull Request由另一人查看后合并。
3. 数据处理负责人维护连接、抓取、解析与切分；业务负责人维护权限、发布、问答、人工接待与SLA。具体模块归属在源码比较后确认。
4. models.py、迁移、统一文档结构和对接协议先共同确认，避免两边同时改出不兼容结构。
5. 两人各自有本机配置与开发账号。密码、真实企业文件、数据库快照、运行日志、模型权重不进入Git。
6. 两人可共用专用联调库，但分别使用约定账号；日常测试使用隔离测试库。数据库变更由约定负责人执行一次。
7. 对接通过经授权的服务接口或统一结构完成，不能靠直接向现有知识表插入数据绕过租户与审核流程。
8. Git上传/拉取不等于数据库同步或发布。拉取后检查依赖、迁移和配置，再部署对应服务。

第一项合作任务：同事处理一份合成产品文档，保留来源、标题、单位与位置；本系统接收为未发布候选，再完成预览、试问、发布和客户回答核对。不要用真实订单作为第一轮联调样本。
''')
write('docs/数据处理对接草案.md','''
# 数据处理对接草案（尚未实现跨项目接入）

此文档是两人约定接口的起点，不代表同事项目已接通。

已有流程：原件上传 → 独立解析任务 → 标准化 → 分块与Embedding → 员工试问 → 审核发布 → 客户检索与回答。
现有实现位于mvp/document_parser.py、knowledge_documents.py、knowledge_routes.py与knowledge_engine.py。

统一标准文档目前使用schema=salescare.document.v1，包含title、filename、format、blocks、issues、characters与parser_version。内容块有id、type、text和location；表格保留rows和header。

同事输出资料时至少应保留：来源标识/URL、原件摘要、文档版本、标题、正文、页码或工作表/行号/幻灯片位置、解析错误与解析器版本。
切分结果保留：所属文档与版本、片段编号、父标题、正文、位置及内容摘要。型号、表头、单位与否定条件不能丢失。

tenant_id、员工身份、渠道范围与发布状态由本系统根据认证和权限决定，不接受外部客户端任意指定后直接生效。新数据进入候选版本，高风险审核和内部/公开用途保持原有规则。

Embedding输出需约定模型标识、维度、索引指纹与版本。不同模型或不同维度不能混用；原文换版后重新构建候选索引。
若同事已有向量库，先约定查询适配接口与权限过滤，不能把连接成功等同可直接替换现有检索。

现有上传接口/api/kb/documents/upload接收原始文件，不是外部预切片JSON接口。接入预处理结果需要新增适配与校验，目前未添加该接口。订单等动态数据应通过独立、鉴权的业务查询接口接入。
''')

for name in ('同事项目','对接资料','发布包'):(WORKSPACE/name).mkdir(parents=True,exist_ok=True)
(WORKSPACE/'同事项目/README.md').write_text('此处预留同事项目。收到源码后另建源码子目录，或将Git仓库克隆到新的子目录。不要把她的运行数据库、密码或配置复制到这里。\n','utf-8')
(WORKSPACE/'对接资料/README.md').write_text('存放双方确认的接口说明、分工、标准样例与验收记录。初始草案见../SalesCare/docs/数据处理对接草案.md。不要存放账号密码。\n','utf-8')
(WORKSPACE/'README.md').write_text('''# 智能客服协作目录

- SalesCare：当前客服项目的独立、干净源码副本，可作为共同仓库候选。
- 同事项目：预留对方源码，收到后放在新的源码子目录。
- 对接资料：双方约定的接口、任务与样例。
- 发布包：准备上传或传递的源码压缩包。

先查看SalesCare/README.md。此目录没有迁移原项目数据库或密钥，也没有切换现有运行服务。
不要把整个协作目录当成一个代码仓库；SalesCare和后续同事项目应分别管理，再决定模块整合方式。
''','utf-8')

# Bounded scan: report paths only, never disclose candidate secret values.
alerts=[]
patterns=[r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',r'\bgh[pousr]_[A-Za-z0-9]{30,}',r'\bgithub_pat_[A-Za-z0-9_]{40,}',r'\bsk-[A-Za-z0-9_-]{24,}',r'(?i)"(?:database_url_sealed|admin_database_url_sealed|setup_token_sealed)"\s*:\s*"[A-Za-z0-9+/=_-]{30,}"']
for p in PROJECT.rglob('*'):
    if not p.is_file():continue
    text=p.read_text('utf-8-sig')
    if any(re.search(pattern,text) for pattern in patterns):alerts.append(str(p.relative_to(PROJECT)))
if alerts:raise SystemExit('Review potential credentials in: '+', '.join(alerts))

entries=[]
for p in sorted(PROJECT.rglob('*')):
    if p.is_file():entries.append({'path':p.relative_to(PROJECT).as_posix(),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
target=WORKSPACE/'发布包/SalesCare-GitHub-source-20261014.zip'
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
    for e in entries:z.write(PROJECT/e['path'],'SalesCare/'+e['path'])
with zipfile.ZipFile(target) as z:
    assert z.testzip() is None
    assert not any(any(part in EXCLUDED or part=='.git' for part in Path(n).parts) for n in z.namelist() if '/docs/' not in n)
report={'source':str(SOURCE),'workspace':str(WORKSPACE),'project':str(PROJECT),'archive':str(target),'files':len(entries),'archive_bytes':target.stat().st_size,'credential_pattern_alerts':alerts,'original_runtime_retained':True,'files_manifest':entries}
(WORKSPACE/'发布包/源码打包核对.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps({k:report[k] for k in ('workspace','project','archive','files','archive_bytes','credential_pattern_alerts')},ensure_ascii=False))
