"""Read-only projections of existing task state and source-backed handoff context."""
from sqlalchemy import case,or_,func
from .models import Task

STAGES={'waiting':'待接待','first_reply':'待首回复','followup':'跟进中','appointment':'预约未到时','overdue':'超时待处理','closed':'已结束','ai':'AI接待'}

def missing_cycle_contact_expression():
    return or_(Task.attempted_at.is_(None),Task.attempted_at<func.coalesce(Task.start_at,Task.accepted_at))

def has_cycle_contact(task):
    return task.attempted_at is not None and task.attempted_at>=(task.start_at or task.accepted_at)

def stage_expression(now):
    return case((Task.status.in_(['cancelled','closed']),'closed'),
                ((Task.mode=='scheduled')&(Task.start_at>now),'appointment'),
                ((Task.deadline_at<now)&missing_cycle_contact_expression(),'overdue'),
                (Task.status=='pending','waiting'),
                ((Task.status=='claimed')&missing_cycle_contact_expression(),'first_reply'),
                (Task.status=='claimed','followup'),else_='ai')

def task_stage(task,now):
    if not task:return 'ai'
    if task.status in ('cancelled','closed'):return 'closed'
    if task.mode=='scheduled' and task.start_at and task.start_at>now:return 'appointment'
    if task.deadline_at and task.deadline_at<now and not has_cycle_contact(task):return 'overdue'
    if task.status=='pending':return 'waiting'
    if task.status=='claimed':return 'first_reply' if not has_cycle_contact(task) else 'followup'
    return 'ai'

def task_presentation(task,owner,now):
    stage=task_stage(task,now)
    online=bool(owner and owner.active and owner.available and owner.heartbeat_at>=now-90)
    return {'queue_stage':stage,'queue_label':STAGES[stage],
            'owner_name':owner.name if owner else '', 'owner_online':online,
            'needs_first_reply':bool(task.status=='claimed' and not has_cycle_contact(task)),
            'owner_attention':bool(task.status=='claimed' and not online),
            'server_now':now}

def handoff_summary(messages,profile):
    # Call only after the normal role/owner privacy masking. No inferred customer facts.
    visitors=[m for m in messages if m['role']=='visitor']
    replies=[m for m in messages if m['role'] in ('ai','human')]
    def source(message):
        if not message:return None
        return {'message_id':message['id'],'sequence':message['sequence'],'text':message['text'][:280]}
    last=visitors[-1] if visitors else None
    last_reply=replies[-1] if replies else None
    return {'latest_question':source(last),'latest_reply':source(last_reply),
            'awaiting_reply':bool(last and (not last_reply or last['sequence']>last_reply['sequence'])),
            'confirmed_needs':[{'label':v.get('label',''),'display':v.get('display',''),'evidence':v.get('evidence','')}
                               for v in profile.values() if v.get('confirmed')],
            'basis':'用户原话及已发送消息，不是自动推断的客户信息'}

def notification_current(task,event):
    if not event.task_id:return True
    if not task or task.status in ('cancelled','closed') or event.version!=task.version:return False
    if event.kind=='sla':return not has_cycle_contact(task)
    if event.kind in ('new_task','transfer'):return task.status=='pending'
    return True
