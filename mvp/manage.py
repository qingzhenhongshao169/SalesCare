"""Local operator commands; no public self-service tenant provisioning."""
import argparse
import json
import secrets
from sqlalchemy import select
from .db import transaction
from .models import Tenant,CompanySettings,ModelConfig,Invitation,Audit,Member,AuthSession
from .security import digest,hash_password

def recover_admin(session,tenant_id,username,password):
    """Local OS operator only; revoke all previous sessions and never print a password."""
    member=session.scalar(select(Member).where(Member.tenant_id==tenant_id,Member.username==username).with_for_update())
    if not member or not member.active or 'admin' not in member.roles:
        raise ValueError('An active enterprise administrator is required.')
    member.password_hash=hash_password(password)
    for row in session.scalars(select(AuthSession).where(AuthSession.tenant_id==tenant_id,AuthSession.identity==member.id,AuthSession.kind=='employee')):
        row.revoked=True
    session.add(Audit(tenant_id=tenant_id,actor='local-operator',action='account.admin_recovered',target=member.id))

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create-tenant');create.add_argument('--slug',required=True);create.add_argument('--name',required=True);create.add_argument('--admin',required=True);create.add_argument('--admin-name',required=True)
    recovery=sub.add_parser('recover-admin');recovery.add_argument('--slug',required=True);recovery.add_argument('--username',required=True)
    pilot=sub.add_parser('publish-pilot');pilot.add_argument('--slug',required=True)
    args=parser.parse_args()
    if args.command=='create-tenant':
        import re
        if not re.fullmatch('[a-z0-9-]{3,80}',args.slug) or not re.fullmatch('[a-z0-9._-]{3,80}',args.admin):raise SystemExit('Invalid tenant or account identifier')
        with transaction() as session:
            if session.scalar(select(Tenant).where(Tenant.slug==args.slug)):raise SystemExit('Tenant already exists; no data changed.')
            tenant=Tenant(slug=args.slug,name=args.name);session.add(tenant);session.flush()
        token=secrets.token_urlsafe(40)
        with transaction(tenant.id) as session:
            session.add(CompanySettings(tenant_id=tenant.id,id='primary',source_hosts=[]))
            session.add(ModelConfig(tenant_id=tenant.id,id='primary',config={}))
            session.add(Invitation(tenant_id=tenant.id,username=args.admin,name=args.admin_name,roles=['admin','editor','publisher'],token_hash=digest(token),expires_at=__import__('time').time()+86400))
            session.add(Audit(tenant_id=tenant.id,actor='local-operator',action='tenant.created',target=tenant.id))
        from .config import RUNTIME,settings
        # This token is a one-time account credential; only its protected local file is returned.
        file=RUNTIME/('invite-'+args.slug+'.txt')
        file.write_text(settings()['public_origin']+'/invite/'+args.slug+'#'+token,'utf-8')
        print('Enterprise created. One-time administrator invitation stored in protected runtime:',file.name)
    elif args.command=='recover-admin':
        from getpass import getpass
        with transaction() as session:
            tenant=session.scalar(select(Tenant).where(Tenant.slug==args.slug,Tenant.active==True))
            if not tenant:raise SystemExit('Enterprise not found.')
        password=getpass('New administrator password (12–128 characters): ')
        if password!=getpass('Confirm password: '):raise SystemExit('Passwords do not match; no data changed.')
        with transaction(tenant.id) as session:
            recover_admin(session,tenant.id,args.username,password)
        print('Administrator password updated locally. Previous sessions revoked; recovery audited.')
    elif args.command=='publish-pilot':
        from .config import settings
        from .models import Knowledge
        if settings()['mode']!='lan-pilot':raise SystemExit('This command only publishes an internal pilot entry.')
        with transaction() as session:
            tenant=session.scalar(select(Tenant).where(Tenant.slug==args.slug,Tenant.active==True))
            if not tenant:raise SystemExit('Enterprise not found.')
        with transaction(tenant.id) as session:
            if not session.scalar(select(Knowledge).where(Knowledge.tenant_id==tenant.id,Knowledge.status=='published',Knowledge.visibility=='public')):
                raise SystemExit('Publish verified public knowledge before opening the entry.')
            session.get(CompanySettings,(tenant.id,'primary')).published=True
            session.add(Audit(tenant_id=tenant.id,actor='local-operator',action='company.pilot_published',target=tenant.id))
        print('Internal pilot customer entry published. Immediate contact remains conditional on qualified online staff and service hours.')

if __name__=='__main__':main()
