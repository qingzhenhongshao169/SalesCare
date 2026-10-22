"""Bounded user-evidence task plan shared by reception, recovery and profile.

No model calls, company facts, executable instructions or inferred commitments.
"""
import re
from .requirements import budget_update
from .project_scope import project_history, restart_position

DIRECTIONS = {
    '企业官网': r'官网|网站|建站|网页',
    '管理系统': r'CRM|ERP|WMS|OA系统|管理系统|业务系统|调度系统|软件系统',
    '小程序': r'小程序|微商城',
    '软件开发': r'软件开发|定制软件|APP',
    'GEO推广': r'GEO|AI搜索|生成式',
    '数字营销': r'数字营销|SEM|SEO|代运营',
}


def direction_mentions(text):
    text=re.sub(r'[^，,。；;]*(?:以后|后面|后续再|明年)[^，,。；;]*','',text)
    found=[]
    for label,pattern in DIRECTIONS.items():
        for m in re.finditer(pattern,text,re.I):
            before=re.split(r'[，,。；;]',text[max(0,m.start()-10):m.start()])[-1]
            after=text[m.end():]
            if not re.search(r'不做|不是|不要|不考虑|暂不|先不|不需要|不建|取消',before) and not re.match(r'\s*(?:暂时|先)?(?:不做|不建|不考虑|不要|取消)',after):
                found.append(label);break
    if '企业官网' in found and re.search(r'还是\s*系统|系统\s*还是',text):found.append('管理系统')
    if not found and re.search(r'(?:做|开发|建设)(?:一个|个)?系统',text):found.append('管理系统')
    return list(dict.fromkeys(found))


def direction_state(history):
    state={'status':'unknown','candidates':[],'evidence':''}
    for m in project_history(history):
        if m['role'] not in ('user','visitor'):continue
        text=m.get('content',m.get('text',''))
        if re.search(r'假如|假设|如果',text):continue
        direction_text=re.sub(r'(?:预算|费用|工期|上线时间)[^，,。；;]*','',text)
        undecided=bool(re.search(r'还没定|没决定|未决定|未定|没想好|不确定|拿不准|没选好|二选一',direction_text))
        mentions=direction_mentions(text)
        if mentions and (undecided or '还是' in text):
            state={'status':'undecided','candidates':mentions,'evidence':text};continue
        selected=[]
        for clause in re.split(r'[，,。；;\n]',text):
            if re.search(r'以后|后面|后续再|明年',clause):continue
            for label,pattern in DIRECTIONS.items():
                for match in re.finditer(pattern,clause,re.I):
                    before=clause[:match.start()];after=clause[match.end():]
                    cancelled=bool(re.search(r'(?:不做|不建|不是|不要|不考虑|不需要|取消|暂不|先不)(?:做|建|开发)?\s*$',before) or re.match(r'\s*(?:暂时|先)?(?:不做|不建|不要|不考虑|取消)',after))
                    if cancelled:
                        state={'status':'unknown' if len(state['candidates'])<=1 else 'multiple',
                               'candidates':[x for x in state['candidates'] if x!=label],'evidence':text}
                        if len(state['candidates'])==1:state['status']='selected'
            options=direction_mentions(clause)
            # Explicit wishes can coexist with a later question. A capability or
            # case question alone is a retrieval topic, not a customer decision.
            wish=bool(re.search(r'(?:想|要|希望|准备|计划|决定|确定|先|就|改|选择|打算|需要).{0,8}(?:做|建|开发|搭建|定制|制作)|^(?:我们)?(?:做|建|开发|搭建|定制|制作)(?:个|一个)?',clause))
            question=bool(re.search(r'你们|贵公司|能不能|能否|是否|能做|可以做|哪些|什么|怎么|如何|案例|作品|做过|吗|么|[？?]',clause))
            if options and (wish or not question):selected.extend(options)
        if selected:
            selected=list(dict.fromkeys(selected))
            state={'status':'multiple' if len(selected)>1 else 'selected','candidates':selected,'evidence':text}
        elif undecided and state['candidates'] and re.search(r'方向|网站|系统|做什么|哪个|选择',text):
            state={**state,'status':'undecided','evidence':text}
    return state


def asks_for_cases(text):
    current=re.split(r'后面|以后|将来|后续再|下一阶段',text)[0]
    if re.search(r'(?:别|不要|不用|不看|不需要).{0,8}(?:案例|作品|推荐)|投诉|转人工|不相关|为什么.{0,8}推荐|打不开|失效|假如|假设|如果',current):return False
    case=bool(re.search(r'案例|作品|项目经验|相关经验',current))
    request=bool(re.search(r'给(?:我|我们)?(?:个|一个|些|点)?|看看|看下|想看|发|提供|介绍|推荐|分享|参考|有.{0,10}(?:吗|么|哪些|什么|[？?])',current))
    experience=bool(re.search(r'(?:你们|贵公司).{0,8}做过.{0,15}(?:吗|么|[？?])',current))
    return case and request or experience


def build_turn_plan(history):
    history=project_history(history)
    current=next((m.get('content',m.get('text','')) for m in reversed(history) if m['role'] in ('user','visitor')),'')
    direction=direction_state(history)
    tasks=[]
    if restart_position(current) is not None:tasks.append('project_restart')
    user_messages=[m for m in history if m['role'] in ('user','visitor')]
    previous_direction=direction_state(user_messages[:-1])
    if direction['status']=='selected' and (previous_direction['status']=='undecided' or
            previous_direction['candidates'] and previous_direction['candidates']!=direction['candidates']):tasks.append('confirm_direction')
    if direction['status']=='unknown' and previous_direction['candidates'] and direction['evidence']==current:tasks.append('cancel_direction')
    if direction['status']=='unknown' and re.search(r'数字化升级|数字化转型',current) and not re.search(r'你们|贵公司|服务范围|业务范围',current):tasks.append('clarify_goal')
    if re.search(r'准备|提供|整理|哪些|什么|清单',current) and re.search(r'资料|材料|素材',current):tasks.append('materials')
    if asks_for_cases(current):tasks.append('cases')
    if direction['status']=='undecided':tasks.append('compare_directions')
    budget=budget_update(current)
    budget_status='unknown';budget_evidence=''
    for m in history:
        if m['role'] not in ('user','visitor'):continue
        text=m.get('content',m.get('text',''))
        update=budget_update(text,previous_budget=budget_status if budget_status=='set' else None)
        if update:budget_status='unset' if update['action']=='clear' else 'set';budget_evidence=text
    clauses=re.split(r'[。；;\n]',current)
    # An explicitly unset customer budget is not a request for supplier prices.
    if budget and budget['action']=='clear':
        clauses=[re.sub(r'预算[^，,。；;]*(?:没定|未定|不确定|不设)[^，,。；;]*','',x) for x in clauses]
    query=' '.join(x.strip(' ，,') for x in clauses if x.strip(' ，,')) or current
    return {'version':'turn-plan-v1','tasks':tasks,'direction':direction,
            'budget_status':budget_status,'budget_evidence':budget_evidence,
            'retrieval_query':query,'case_evidence':[]}


def coverage_issue(answer,plan):
    tasks=plan.get('tasks',[]);cases=plan.get('case_evidence',[])
    if 'project_restart' in tasks and not re.search(r'新项目|重新|重开',answer):return 'missing_project_restart'
    if 'cancel_direction' in tasks and not re.search(r'取消|不做|暂停|先不|暂缓|不再|搁置|放下',answer):return 'missing_direction_cancellation'
    if 'confirm_direction' in tasks and not any(re.search(DIRECTIONS[label],answer,re.I) for label in plan['direction']['candidates']):return 'missing_selected_direction'
    if 'clarify_goal' in tasks and re.search(r'服务分为|主要提供|四类|四大|先从官网|先做官网',answer):return 'premature_solution'
    if 'compare_directions' in tasks:
        if re.search(r'先从官网|先做官网|先建官网|官网先承担|您(?:想|要)做(?:企业)?官网',answer):return 'premature_direction'
        candidates=plan.get('direction',{}).get('candidates',[])
        if len(candidates)>=2:
            if not all(re.search(DIRECTIONS.get(label,re.escape(label)),answer,re.I) for label in candidates):return 'missing_comparison'
        elif not re.search(r'目标|场景|解决|哪|什么',answer):return 'missing_comparison'
    if 'materials' in tasks:
        categories=[r'公司介绍|企业介绍|企业简介',r'产品|服务资料|服务介绍',r'联系方式|联系信息|品牌素材|Logo']
        if sum(bool(re.search(p,answer,re.I)) for p in categories)<2:return 'missing_materials'
    if 'cases' in tasks and cases:
        if re.search(r'没有.{0,12}(?:案例|项目经验)|不能提供.{0,6}案例|未.{0,8}找到.{0,8}案例',answer):return 'false_no_case'
        if not any(c['title'] in answer or c['url'] in answer for c in cases):return 'missing_case'
    if 'cases' in tasks and not cases and 'compare_directions' not in tasks:
        if not re.search(r'(?:未|没有|暂无|尚未|需.{0,4}核实).{0,20}(?:案例|经验|做过)|(?:案例|经验).{0,20}(?:未|没有|尚未|核实)',answer):return 'missing_case_boundary'
    return None


def task_fallback(plan,records):
    """Complete planned tasks only from currently authorized case evidence."""
    tasks=plan.get('tasks',[])
    if not tasks:return None
    parts=[];used=[]
    if 'project_restart' in tasks:
        chosen=plan['direction']['candidates']
        parts.append('好的，这次按新项目重新梳理。'+('先按'+ '、'.join(chosen)+'方向继续。' if chosen else '您这次想做什么，或者最希望解决什么问题呢？'))
    if 'cancel_direction' in tasks:
        parts.append('好的，先暂停这个方向，暂不按已选需求继续推荐。之后想继续或调整方向，告诉我就好。')
    if 'confirm_direction' in tasks:
        chosen=plan['direction']['candidates'][0]
        parts.append('好的，先按'+chosen+'方向梳理。'+('您最想先改善哪一段内部业务流程呢？' if chosen=='管理系统' else '您最希望它先解决什么问题呢？'))
    if 'clarify_goal' in tasks:parts.append('可以呀，您现在最想改善的是获客、客户跟进，还是内部业务流程呢？先从一个最困扰您的问题聊起就好。')
    if 'compare_directions' in tasks:
        descriptions={
            '企业官网':'官网侧重对外展示和获取咨询',
            '管理系统':'管理系统侧重订单、人员协作等内部流程',
            '小程序':'小程序可围绕平台内的使用场景梳理',
            '软件开发':'软件开发需要先明确使用端、核心功能和维护需求',
            'GEO推广':'GEO推广可先关注AI搜索中的信息呈现目标',
            '数字营销':'数字营销可先明确目标受众、渠道和转化目标',
        }
        candidates=plan.get('direction',{}).get('candidates',[])
        points=[descriptions[x] for x in candidates if x in descriptions]
        if '软件开发' in candidates and re.search(r'APP',plan['direction'].get('evidence',''),re.I):
            points=[x.replace('软件开发需要','APP需要') for x in points]
        if len(points)>=2:parts.append('方向还没确定，可以先按使用场景比较：'+'；'.join(points)+'。')
        else:parts.append('方向还没确定，可以先从业务目标和使用场景梳理。')
    if 'materials' in tasks:
        parts.append('资料可以先准备公司介绍、产品或服务资料、品牌素材、联系方式，以及喜欢的参考网站；还没备齐也可以先列已有的。')
        used.extend([r for r in records if re.search(r'公司介绍|企业介绍',r['content']) and re.search(r'产品|服务资料',r['content'])][:1])
    if 'cases' in tasks and 'compare_directions' not in tasks:
        available={r['id']:r for r in records}
        cases=[c for c in plan.get('case_evidence',[]) if c['id'] in available]
        if cases:
            for c in cases[:2]:
                parts.append('相近方向可以参考「'+c['title']+'」：'+c['summary']+' 官网案例：'+c['url']+'。')
                used.append(available[c['id']])
            parts.append('历史经验供参考，具体建设范围需要结合您的需求评估。')
        else:parts.append('这次没有找到可核实且匹配当前需求的公开案例，具体项目经验还需要进一步核实。')
    if 'compare_directions' in tasks:parts.append('您希望先解决什么具体问题，主要给哪些人使用呢？')
    return ''.join(parts),list({r['id']:r for r in used}.values())
