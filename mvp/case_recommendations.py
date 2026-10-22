"""Conservative, reviewed case attachments. No model call, price inference or live crawl."""
import json
import re
from urllib.parse import urlsplit
from sqlalchemy import select
from .models import CompanySettings, Knowledge, Message
from .knowledge_provenance import digest
from .answering import social_intent

SERVICES = {
    'website': ('网站建设', r'官网|网站|建站|独立站'),
    'miniapp': ('小程序开发', r'小程序|微商城'),
    'software': ('软件系统开发', r'CRM|ERP|WMS|OA系统|管理系统|业务系统|软件(?:开发|定制|系统)'),
    'marketing': ('数字营销', r'数字营销|新媒体|代运营|SEO|SEM'),
    'geo': ('GEO服务', r'GEO|生成式引擎优化|AI搜索优化'),
}
INDUSTRIES = {
    'industrial': ('工业制造', r'工业|制造业|机械|设备|激光'),
    'energy': ('电力能源', r'电力|能源|电网'),
    'logistics': ('物流供应链', r'物流|供应链|货运'),
    'architecture': ('建筑设计', r'建筑|城乡设计|设计院'),
    'appliance': ('家电', r'家电|冰箱|电器'),
    'home': ('家居', r'家居|家具|全屋定制|厨房'),
    'education': ('教育科研', r'高校|大学|科研|学校'),
    'fitness': ('健身服务', r'健身|运动场馆'),
}
GOALS = {
    'export': ('面向海外', r'外贸|海外|出海|英文|多语言|国际站'),
    'branding': ('展示品牌', r'品牌|形象|公司介绍|企业介绍'),
    'products': ('展示产品', r'产品展示|展示产品|展示设备|设备展示|产品资料'),
    'membership': ('会员活动', r'会员|积分|用户社区|社群'),
    'after_sales': ('售后服务', r'售后|报修|客户服务'),
    'orders': ('在线交易', r'商城|下单|支付|订单'),
    'appointments': ('预约服务', r'预约|订课|排课'),
}
DEFAULT_POLICY = {'enabled': True, 'min_business_turns': 3, 'max_items': 1,
                  'weights': {'service': 35, 'goals': 30, 'industry': 25, 'priority': 10}}
ASK_CASE = re.compile(r'案例|作品|做过.{0,8}(?:类似|相关)|类似.{0,6}项目', re.I)
DECLINE = re.compile(r'别推荐|不要.{0,8}(?:推荐|案例)|不(?:想|用|需要|看).{0,5}(?:案例|推荐)|只(?:要|需).{0,8}(?:回答|答案)')
BLOCK = re.compile(r'投诉|骗人|垃圾|转人工|人工客服|找.{0,3}顾问|不满意|忽略规则|提示词|密钥|其他(?:企业|租户)|内部资料')
OUT_OF_SCOPE = re.compile(r'天气|气温|下雨|彩票|股票代码|讲个笑话')
BUSINESS = re.compile('|'.join(v[1] for taxonomy in (SERVICES, INDUSTRIES, GOALS) for v in taxonomy.values()) +
    r'|预算|费用|报价|价格|上线|交付|工期|素材|资料|功能|页面|内容|需求|设计|开发|定制|方案|维护|团队|展示|服务|首期|阶段|客户|公司|产品|订单|范围|素材|具体呢|怎么做|怎么开始|多久|多少钱|需要准备|有哪些')


def recommendation_intent(text):
    """A turn threshold is only a gate; the current request must invite evidence.

    Prefer omission over interrupting corrections, operations or practical FAQs.
    This bounded intent policy is not a general semantic intent classifier.
    """
    current=re.split(r'后面|以后|将来|后续再|下一阶段',text)[0]
    from server.turn_plan import asks_for_cases
    if DECLINE.search(text) or BLOCK.search(text) or re.search(r'如果|假如|假设',text):return None
    if re.search(r'(?:案例|作品|链接).{0,8}(?:打不开|失效|报错|错误|不对|无法访问)',current):return None
    if re.search(r'为什么.{0,8}(?:推荐|发|展示)|怎么.{0,4}(?:又|老是).{0,5}(?:推|发)|不相关|没问案例|没让.{0,6}推荐',current):return None
    if asks_for_cases(current):return 'explicit_case'
    request=re.search(r'看看|看一下|看下|想看|想了解|给我|给我们|发我|发一个|提供|推荐|分享|介绍|展示|参考|有没有|有哪些|有无|有什么|有相关|有类似|有.{0,5}(?:吗|么|[？?])',current)
    experience=re.search(r'(?:你们|贵公司|公司).{0,10}做过.{0,14}(?:吗|么|[？?])',current)
    if experience or ASK_CASE.search(current) and request:return 'explicit_case'
    # An acknowledgement or a field correction is not a recommendation request.
    if re.search(r'预算|报价|费用|价格|多少钱|工期|排期|上线|交付|保证|承诺',current):return None
    if re.search(r'(?:你们|贵公司).{0,15}(?:经验|实力|擅长|能力|能做|可以做|能不能做)',current):
        if re.search(r'吗|么|如何|怎样|怎么样|什么|介绍|看看|[？?]',current):return 'capability_question'
    if re.search(r'(?:你们)?(?:能做|可以做|能不能做).{0,5}(?:吗|么|[？?])',current):return 'capability_question'
    design_subject=r'官网|网站|小程序|品牌|形象|展示(?:设备|产品|公司)|会员|售后|报修|设计|布局'
    if re.search(design_subject,current) and re.search(r'怎么(?:做|设计|规划|展示)|如何(?:做|设计|规划|展示)|怎样(?:做|设计|规划|展示)|有什么建议|什么方案|怎么建议|给.{0,4}(?:建议|方案)|推荐.{0,4}方案',current):
        return 'solution_question'
    return None


def signature(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True))


def safe_url(url, allowed_hosts):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.hostname in allowed_hosts and not p.username
                and not p.password and p.port in (None, 443) and not p.query and not p.fragment
                and not any(c in url for c in '\r\n\\'))
    except (ValueError, TypeError):
        return False


def policy(value):
    value = value if isinstance(value, dict) else {}
    return {**DEFAULT_POLICY, **value, 'weights': {**DEFAULT_POLICY['weights'], **value.get('weights', {})}}


def _matches(text, taxonomy):
    found = []
    for key, (_, pattern) in taxonomy.items():
        for match in re.finditer(pattern, text, re.I):
            prefix = text[max(0, match.start()-10):match.start()]
            clause = re.split(r'[，,。；;]', prefix)[-1]
            if not re.search(r'不做|不是|不要|不需要|不考虑|取消|暂不|并非|假如|假设|如果', clause):
                found.append(key)
                break
    return found


def context(messages):
    """Use only visitor evidence through this task's sequence, never mutable profile guesses."""
    from server.project_scope import project_history
    messages=project_history(messages)
    state = {'service': None, 'industries': [], 'goals': [], 'evidence': [],
             'turns': 0, 'declined': False, 'explicit': False, 'blocked': True}
    subjects = {}
    pending = {'industries': [], 'goals': [], 'evidence': []}
    seen = set()
    for m in messages:
        if m.get('role') not in ('visitor', 'user'):
            continue
        text = m.get('text', m.get('content', '')).strip()
        if not text:
            continue
        key = re.sub(r'[\s，。！？、,.!?]+', '', text).lower()
        social = bool(social_intent(text))
        turn_intent = recommendation_intent(text)
        business = bool(BUSINESS.search(text) or turn_intent) and not OUT_OF_SCOPE.search(text)
        if not social and business and key not in seen:
            state['turns'] += 1
            seen.add(key)
        current = re.split(r'后面|以后|将来|后续再|下一阶段', text)[0]
        explicit = turn_intent == 'explicit_case'
        if DECLINE.search(text):
            state['declined'] = True
        elif explicit:
            state['declined'] = False
        state['explicit'] = explicit
        state['recommendation_intent'] = turn_intent
        state['blocked'] = social or not turn_intent or bool(BLOCK.search(text)) or state['declined']
        # Future work is not the active project. Ambiguous simultaneous projects suppress auto cards.
        services = _matches(current, SERVICES)
        if state['service'] and re.search(SERVICES[state['service']][1], current, re.I) and state['service'] not in services:
            state['service'] = None
        if re.search(r'另一个项目|另一个官网|另外一个项目|新项目|重新开始|换个项目', current):
            subjects = {}
            pending = {'industries': [], 'goals': [], 'evidence': []}
            state['service'] = None
        if len(services) > 1:
            state['service'] = None
            pending = {'industries': [], 'goals': [], 'evidence': []}
        elif services:
            state['service'] = services[0]
        service = state['service']
        if service:
            if service not in subjects:
                subjects[service] = pending if not subjects else {'industries': [], 'goals': [], 'evidence': []}
                pending = {'industries': [], 'goals': [], 'evidence': []}
            data = subjects[service]
            industries, goals = _matches(current, INDUSTRIES), _matches(current, GOALS)
            data['industries'] = [k for k in data['industries'] if k in industries or not re.search(INDUSTRIES[k][1], current, re.I)]
            data['goals'] = [k for k in data['goals'] if k in goals or not re.search(GOALS[k][1], current, re.I)]
            if industries:
                data['industries'] = industries
            data['goals'] = list(dict.fromkeys(data['goals'] + goals))
            if services or industries or goals:
                data['evidence'].append({'message_id': m.get('id', ''), 'sequence': m.get('sequence', 0),
                                         'text': text[:500]})
            state.update(industries=data['industries'], goals=data['goals'], evidence=data['evidence'][-4:])
        else:
            if not subjects and len(services) <= 1:
                pending['industries'] = _matches(current, INDUSTRIES) or pending['industries']
                pending['goals'] = list(dict.fromkeys(pending['goals'] + _matches(current, GOALS)))
                if pending['industries'] or pending['goals']:
                    pending['evidence'].append({'message_id':m.get('id',''), 'sequence':m.get('sequence',0), 'text':text[:500]})
            state.update(industries=[], goals=[], evidence=[])
    return state


def rank_cases(state, cases, config, sent=()):
    if (not config['enabled'] or state['blocked'] or not state['service']
            or not state['explicit'] and state['turns'] < config['min_business_turns']):
        return []
    weights = config['weights']
    ranked = []
    for item in cases:
        if state['service'] not in item['service_types'] or item['id'] in sent and not state['explicit']:
            continue
        industries = set(state['industries']) & set(item['industries'])
        goals = set(state['goals']) & set(item['goals'])
        if not state['explicit'] and not (industries or goals):
            continue
        score = weights['service'] + weights['industry'] * bool(industries) + weights['goals'] * bool(goals)
        score += weights['priority'] * item.get('priority', 50) / 100
        why = [SERVICES[state['service']][0]]
        why += [INDUSTRIES[k][0] for k in sorted(industries)]
        why += [GOALS[k][0] for k in sorted(goals)]
        ranked.append((score, item['id'], {**item, 'reason': '与您提到的' + '、'.join(why) + '相关，供了解项目经验。',
                                         'score': round(score, 2)}))
    return [item for _, _, item in sorted(ranked, key=lambda x: (-x[0], x[1]))[:config['max_items']]]


def _authorized(session, tid, channel_id, ids, lock=False):
    from .knowledge_retrieval import PostgresScan, revalidate
    config_row = session.get(CompanySettings, (tid, 'primary'))
    if not config_row or not policy(config_row.case_recommendation_policy)['enabled'] or not ids:
        return {}
    records = [r for r, _, _ in PostgresScan(session, tid, channel_id).scan(ids=list(ids))]
    if lock:
        records = revalidate(session, tid, records, channel_id, lock=True)
    result = {}
    for record in records:
        row = session.get(Knowledge, (tid, record['id']), populate_existing=True)
        meta = row.case_recommendation or {}
        if not isinstance(meta, dict) or any(not isinstance(meta.get(key), list) or set(meta[key])-set(taxonomy)
                for key, taxonomy in (('service_types', SERVICES), ('industries', INDUSTRIES), ('goals', GOALS))):
            continue
        if not isinstance(meta.get('priority'), int) or not 0 <= meta['priority'] <= 100:
            continue
        if (not meta.get('enabled') or not meta.get('reviewer_id')
                or meta.get('source_version') != row.version or meta.get('source_digest') != digest(row.content)
                or meta.get('summary', '') not in row.content or not meta.get('summary')
                or meta.get('source_url') != row.source_url
                or not safe_url(row.source_url, config_row.source_hosts)):
            continue
        result[row.id] = {'id': row.id, 'title': row.title, 'summary': meta['summary'], 'url': row.source_url,
                          'version': row.version, 'source_digest': digest(row.content), 'signature': signature(meta),
                          'service_types': meta['service_types'], 'industries': meta['industries'],
                          'goals': meta['goals'], 'priority': meta['priority']}
    return result


def select_for_message(session, c, message, config_row):
    """Called under the existing conversation/tenant/channel commit guards."""
    if c.channel != 'web' or c.state not in ('AI_ACTIVE', 'HANDOFF_PENDING'):
        return []
    config = policy(config_row.case_recommendation_policy)
    if not config['enabled']:
        return []
    later = session.scalar(select(Message.id).where(Message.tenant_id == c.tenant_id,
        Message.conversation_id == c.id, Message.role == 'visitor', Message.sequence > message.sequence).limit(1))
    if later:
        return []
    history = session.scalars(select(Message).where(Message.tenant_id == c.tenant_id,
        Message.conversation_id == c.id, Message.sequence <= message.sequence).order_by(Message.sequence.desc()).limit(201)).all()
    if len(history) > 200:
        return []  # Conservative bound; never silently reconstruct a partial project as complete.
    from .service import redacted_text
    history = list(reversed(history))
    from server.turn_plan import direction_state
    if direction_state([{'role':m.role,'text':m.text} for m in history])['status']=='undecided':return []
    state = context([{'id': m.id, 'role': m.role, 'text': redacted_text(m.text), 'sequence': m.sequence} for m in history])
    if state['blocked'] or not state['service'] or not state['explicit'] and state['turns'] < config['min_business_turns']:
        return []
    previous_turns = [card.get('business_turn', state['turns']) for m in history
                      for card in (m.case_recommendations or []) if card.get('subject_service') == state['service']]
    if not state['explicit'] and previous_turns and state['turns'] - max(previous_turns) < 3:
        return []
    ids = list(session.scalars(select(Knowledge.id).where(Knowledge.tenant_id == c.tenant_id,
        Knowledge.status == 'published', Knowledge.case_recommendation['enabled'].as_boolean() == True).order_by(Knowledge.id).limit(200)))
    authorized = _authorized(session, c.tenant_id, c.channel_id, ids, lock=True)
    sent = {item['id'] for m in history for item in (m.case_recommendations or []) if 'id' in item}
    selected = rank_cases(state, list(authorized.values()), config, sent)
    return [{**item, 'for_message_id': message.id, 'for_sequence': message.sequence,
             'business_turn': state['turns'], 'subject_service': state['service'],
             'evidence': state['evidence'], 'notice': '历史案例供能力参考，具体范围、费用和交期需另行评估。'}
            for item in selected]


def public_cards(session, tid, channel_id, cards):
    """Recheck old message attachments on GET and SSE; preserve originals only in the audit history."""
    if not cards:
        return []
    current = _authorized(session, tid, channel_id, {c.get('id') for c in cards if c.get('id')})
    allowed = ('id', 'title', 'summary', 'url', 'version', 'reason', 'notice', 'for_message_id', 'for_sequence')
    output = []
    for card in cards:
        fresh = current.get(card.get('id'))
        source = session.get(Message,(tid,card.get('for_message_id','')))
        appropriate = source and source.role=='visitor' and recommendation_intent(source.text)
        if (fresh and appropriate and all(card.get(k) == fresh[k] for k in ('version', 'source_digest', 'signature', 'url', 'title', 'summary'))):
            output.append({k: card[k] for k in allowed if k in card})
    return output
