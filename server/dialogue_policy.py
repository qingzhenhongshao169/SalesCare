"""Bounded, user-evidence dialogue planning; never authorizes company facts."""
import re
from difflib import SequenceMatcher
from .answer_policy import TOKEN, quantity

POLICY_VERSION = 'feedback-20261013-v5'
TOPICS = [('企业官网', r'官网|网站|网页|建站'), ('小程序', r'小程序|微商城'),
          ('管理系统', r'CRM|ERP|OA|管理系统|业务系统'), ('软件开发', r'软件|APP'),
          ('GEO推广', r'GEO|AI搜索|生成式'), ('数字营销', r'数字营销|SEM|SEO|代运营')]
BUDGET = re.compile(r'预算(?:上限|是|为|改为|改成|只有|约|大概)?[ \t]*([\d一二两三四五六七八九十百千万,.]+[ \t]*(?:万元|千元|元|万|千)?)')
PAGES = re.compile(r'\d+\s*(?:个(?:独立设计的|独立)?页面|页面|页|条(?:产品|新闻|内容))')
ASKS = {
    'materials': r'资料|文案|图片|素材',
    'pages': r'页面|页数|栏目|内容条数|展示哪些',
    'goal': r'用途|目标|展示公司|展示形象|询价|询盘',
    'timeline': r'上线|时间|工期',
    'budget': r'预算',
}


def user_topics(text):
    found=[]
    for name, pattern in TOPICS:
        if any(not re.search(r'不做|不是|不要|先不做', text[max(0,m.start()-5):m.start()])
               for m in re.finditer(pattern,text,re.I)):
            found.append(name)
    return found


def question_kind(text):
    for key,pattern in ASKS.items():
        if re.search(pattern,text,re.I):return key
    return None


def dialogue_context(history):
    """Only explicit user evidence within the caller's message-sequence boundary."""
    current=next((m['content'] for m in reversed(history) if m['role']=='user'),'')
    facts={}; topic=[]; last_question=''; asked=[]; previous_budget=None
    for m in history:
        text=m['content']
        if m['role']!='user':
            questions=re.findall(r'[^。！？!?\n]*[？?]',text)
            if questions:
                last_question=questions[-1][-240:]
                kind=question_kind(last_question)
                if kind:asked.append(kind)
            continue
        explicit=user_topics(text)
        if explicit:
            if topic and not set(topic).intersection(explicit):
                facts={}; asked=[]; last_question=''
            topic=explicit
        budgets=[match for match in BUDGET.finditer(text)
                 if not re.search(r'不是|并非|没有',text[max(0,match.start()-4):match.start()])]
        if budgets:
            previous_budget=facts.get('budget')
            facts['budget']=budgets[-1].group(0)
        if re.search(r'预算.{0,8}(?:没定|未定|不确定|取消|先不设)',text):facts.pop('budget',None)
        time_match=re.search(r'(?:希望|期望|预计|计划|上线时间|尽快)[^，。；\n]{0,12}?(?:[\d一二两三四五六七八九十]+\s*(?:周|星期|天|个月)|下周|本月|下个月)',text)
        if time_match:facts['timeline']=time_match.group(0)
        if re.search(r'时间.{0,6}(?:没定|未定|不确定)',text):facts.pop('timeline',None)
        goal=re.search(r'展示公司|展示企业|公司形象|品牌展示|展示产品|介绍产品|提交询价|客户询价|收集询盘',text)
        if goal:facts['goal']=goal.group(0)
        pages=PAGES.findall(text)
        if pages:
            if facts.get('pages')!=pages[-1]:facts.pop('page_meaning',None)
            facts['pages']=pages[-1]
        if re.search(r'独立设计|独立页面|条(?:新闻|产品|内容)|(?:新闻|产品|内容)条数',text):
            facts['page_meaning']=text[:200]
        if re.search(r'(?:资料|素材|文案|图片).{0,8}(?:还没|没有|没齐|不齐|未准备|不全)',text):
            facts['materials']='not_ready'
        elif re.search(r'(?:资料|素材|文案|图片).{0,8}(?:都有|备齐|齐全|准备好|现成|齐了)',text):
            facts['materials']='ready'
        elif question_kind(last_question)=='materials' and re.fullmatch(r'(?:是的[，, ]*)?(?:都有了?|齐了|准备好了)[。！! ]*',text):
            facts['materials']='ready'
    budget_changed=bool(previous_budget and facts.get('budget') and previous_budget!=facts['budget'])
    if re.search(r'首次|第一次|初次',current) and re.search(r'准备|要说|说些什么|需要.{0,4}提供',current):scene='first_consultation'
    elif re.search(r'准备|提供|哪些|什么|给哪些',current) and re.search(r'资料|材料|素材|准备什么',current):
        scene='website_materials' if '企业官网' in topic else 'project_materials'
    elif re.search(r'保证|确保|一定|承诺|肯定',current) and re.search(r'排名|询盘|成交|效果|收益',current):scene='effect_boundary'
    elif re.search(r'上线|工期|交付',current) and re.search(r'多久|几天|确定|保证|能否|能不能|(?:能|可以).{0,4}(?:上线|交付)(?:吗|么|[？?])',current):scene='deadline'
    elif re.search(r'预算',current) and (budget_changed or re.search(r'改为|改成|更新|调整',current)):scene='budget_update'
    elif re.search(r'报价|多少钱|收费|价格|费用',current):scene='quotation'
    elif facts and ('企业官网' in topic):scene='website_requirements'
    elif len(topic)>1:scene='multiple_services'
    else:scene='business'
    return {'scene':scene,'topics':topic,'user_requirements':facts,'budget_changed':budget_changed,
            'last_question':last_question,'asked_fields':list(dict.fromkeys(asked)),
            'current_message':current[:1000]}


def quality_issue(answer,history):
    """Reject specific, observable misses, not every repeated word or disclaimer."""
    context=dialogue_context(history);facts=context['user_requirements'];scene=context['scene']
    questions=re.findall(r'[^。！？!?\n]*[？?]',answer)
    if len(questions)>1:return 'multiple_questions'
    if questions:
        kind=question_kind(questions[-1])
        if kind=='materials' and facts.get('materials')=='ready':return 'known_materials'
        if re.search(r'独立.{0,8}页面|独立设计|内容条数',questions[-1]):
            if not facts.get('pages'):return 'unneeded_page_ambiguity'
            if facts.get('page_meaning'):return 'resolved_page_ambiguity'
    if facts.get('budget') and scene in ('budget_update','website_requirements','quotation'):
        for match in re.finditer(r'足够|够用|就能做|(?:通常|一般)?(?:能|可以|可)覆盖|能做出|能实现',answer):
            before=answer[max(0,match.start()-5):match.start()]
            if not re.search(r'不能|无法|未必|不一定|不足以|尚不能',before):return 'budget_coverage_claim'
    if scene=='multiple_services':
        patterns=dict(TOPICS)
        for topic in context['topics']:
            if not re.search(patterns[topic],answer,re.I):return 'missing_service_direction'
    if scene=='first_consultation' and sum(w in answer for w in ('使用角色','核心操作流程','必须功能','现有系统'))>=2:
        return 'high_bar_first_consultation'
    if scene=='website_materials' and not re.search(r'图片格式|logo格式|只准备|只需要|图片需要',context['current_message'],re.I):
        if (not re.search(r'会员|订单|支付|审批|平台|接口|CRM',context['current_message'],re.I)
                and sum(w in answer for w in ('使用角色','核心操作流程','核心流程','必须功能'))>=2):
            return 'generic_materials'
        categories=[r'公司介绍|企业介绍|企业简介|公司简介',r'产品|服务资料|服务介绍|服务说明',r'联系方式|联系信息|地址|电话|邮箱']
        if sum(bool(re.search(pattern,answer)) for pattern in categories)<2:return 'generic_materials'
    if scene=='budget_update':
        if questions and context['last_question']:
            compact_question=lambda text:re.sub(r'方便的话|您目前|您先说说|先说说|您|你|\s|[，。；！？,.!?;]','',text)
            if SequenceMatcher(None,compact_question(questions[-1]),compact_question(context['last_question'])).ratio()>.82:
                return 'repeated_budget_question'
        expected=BUDGET.search(facts.get('budget',''))
        stated=list(BUDGET.finditer(answer))
        if expected and stated and not re.search(r'从.{0,12}(?:改|调整|更新)|原(?:来|先|预算)|之前',answer):
            normalized=lambda match:next((quantity(t) for t in TOKEN.finditer(match.group(1)) if quantity(t)),None)
            if normalized(stated[-1])!=normalized(expected):return 'stale_budget'
        earlier=[m['content'] for m in history[:-1] if m['role']=='assistant']
        compact=lambda text:re.sub(r'[\s，。；！？,.!?;\d一二两三四五六七八九十万元]+','',text)
        if earlier and len(answer)>90 and SequenceMatcher(None,compact(earlier[-1]),compact(answer)).ratio()>.78:
            return 'repeated_explanation'
    if re.search(r'这属于客户需求信息|AI\s*(?:不能|不会|不可以)|拍板承诺|具体交付仍以确认后的方案和合同为准',answer) and scene!='business':
        return 'internal_policy_language'
    return None


def next_website_question(context):
    facts=context['user_requirements'];asked=context['asked_fields']
    if facts.get('materials')=='ready':
        if not facts.get('pages') and 'pages' not in asked:return '您准备设置哪些栏目，或者重点展示哪些产品和服务？'
        if not facts.get('timeline') and 'timeline' not in asked:return '您希望什么时候上线？'
        return ''
    if not facts.get('materials') and 'materials' not in asked:return '您现在有公司介绍、产品图片等现成资料吗？'
    if not facts.get('goal') and 'goal' not in asked:return '官网主要用于展示公司，还是也希望客户提交询价？'
    if not facts.get('pages') and 'pages' not in asked:return '您准备重点展示哪些产品或服务？'
    if not facts.get('timeline') and 'timeline' not in asked:return '您希望什么时候上线？'
    return ''


def scene_fallback(history,records):
    """General preparation/analysis advice with no invented company capability."""
    c=dialogue_context(history);facts=c['user_requirements'];scene=c['scene']
    if scene=='first_consultation':
        return '不用一开始就准备得很完整。您可以先说想解决什么问题、准备做什么，再补充预算和希望上线的时间；还没确定的部分，也可以在咨询中慢慢梳理。',[]
    if scene=='website_materials':
        # Cite only a source that actually supports at least part of the checklist.
        used=[k for k in records if re.search(r'企业介绍|公司介绍|产品.*资料|参考网站',k.get('content',''))][:1]
        return ('可以先整理公司介绍、产品或服务资料、Logo与品牌素材、联系方式，以及喜欢的参考网站。'
                '有现成图片和文案也一起准备；暂时不齐，可以先列出已有的。'+
                ('' if facts.get('goal') else '您希望官网重点展示公司形象，还是也让客户提交询价？')),used
    if scene=='deadline':
        return '您提到的上线时间可以先作为目标，是否能做到还要结合页面、功能和素材准备情况核算。'+(
            '资料已经备齐的话，下一步可以整理建设范围，请顾问评估排期。' if facts.get('materials')=='ready' else '您目前的文案和图片准备好了吗？'),[]
    if scene=='effect_boundary':
        return '排名或效果不能给您作无条件保证，结果还会受到竞争、内容基础和平台机制影响。可以先梳理您希望在哪些问题下被客户看到，再评估优化方向。',[]
    if scene=='multiple_services':
        descriptions={'企业官网':'官网用于品牌与产品展示','小程序':'小程序可按实际需求规划用户操作入口',
                      '管理系统':'CRM等管理系统用于梳理客户跟进与内部业务流程','软件开发':'软件开发先明确角色与业务流程',
                      'GEO推广':'GEO可围绕AI搜索中的品牌信息梳理内容','数字营销':'数字营销先明确目标客户和获客路径'}
        return '可以先按不同方向梳理需求：'+ '；'.join(descriptions[t] for t in c['topics'])+'。这些是规划思路，具体服务范围和对接方案需要核实。您希望先推进哪一部分？',[]
    if scene=='budget_update':
        budget=facts.get('budget','最新预算')
        subject='官网' if '企业官网' in c['topics'] else '项目'
        return '好的，您的'+subject+budget+'，我会按最新需求继续梳理。'+(next_website_question(c) if subject=='官网' else ''),[]
    if '企业官网' in c['topics'] and (facts or scene=='quotation'):
        parts=[facts[k] for k in ('goal','budget','timeline') if k in facts]
        intro='明白，您想做企业官网'+('，您提到'+ '、'.join(parts) if parts else '')+'。'
        if facts.get('pages') and not facts.get('page_meaning'):
            return intro+'您说的“'+facts['pages']+'”是指各自独立设计的页面，还是产品、新闻等内容条数？这会影响工作量，实际报价与排期需要核算。',[]
        if facts.get('materials')=='ready':
            return intro+'既然资料已备齐，下一步可以整理栏目、内容清单与功能范围，再请专业顾问核算报价和排期。'+next_website_question(c),[]
        advice=('报价主要看页面设计、功能范围和内容准备情况，具体金额需要结合建设范围核算。' if scene=='quotation' else
                '可以先把展示内容和功能梳理成建设范围，再评估费用与排期。')
        return intro+advice+next_website_question(c),[]
    return None
