"""Shared parsing of explicit customer wishes, never supplier quotes or promises."""
import re
from .answer_policy import TOKEN, quantity

AMOUNT = r'(?:\d[\d,]*(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)\s*(?:万元|千元|元|万|千|[kKwW](?:元)?|块钱?)?(?![\dA-Za-z])'
CHANGE = r'变成|变为|变到|变|改为|改成|改到|调整为|调整到|更新为|降到|提高到|增加到|减少到'
MODIFIERS = r'(?:(?:可能|目前|现在|大概|大约|大致|暂时|暂定|只有|只剩|仅有|仅|约|是|为|'+CHANGE+r'|控制在|上限|最多|不超过|就|也就)\s*){0,6}'
# group(1) remains the amount for callers inspecting budget language.
BUDGET = re.compile(r'预算\s*'+MODIFIERS+r'('+AMOUNT+r')', re.I)
PERIOD = r'(?:\d+|[一二两三四五六七八九十]+)\s*(?:周|星期|天|个月)|下周|本月|下个月|\d{4}年\d{1,2}月\d{1,2}日'
TIMELINE = re.compile(r'(?:希望|期望|预计|计划|上线时间|交付时间|工期)[^，。；\n\d]{0,10}?('+PERIOD+r')|('+PERIOD+r')\s*(?:内)?(?:上线|交付|做完|完成)')


def _money(raw):
    raw=raw.strip()
    # w is a monetary shorthand only inside a budget statement. Globally W
    # may mean watts, so do not teach the general numeric guard that W=money.
    raw=re.sub(r'\s*[wW](?:元)?$', '万', raw)
    match=TOKEN.fullmatch(raw)
    if not match:return None
    value=quantity(match)
    if value is None and not match['unit']:
        match=TOKEN.fullmatch(raw+'元');value=quantity(match) if match else None
    if not value or value[0]<0 or value[1] not in ('money',''):return None
    return float(value[0])


def budget_update(text, *, previous_budget=None):
    """Return set/clear or None; preserve original evidence outside this parser."""
    if re.match(r'\s*(?:(?:那|那么|请问)[，,\s]*)?(?:如果|假如|假设)',text):return None
    found=[]
    for match in BUDGET.finditer(text):
        prefix=text[max(0,match.start()-10):match.start()]
        suffix=text[match.end():match.end()+12]
        clause_prefix=re.split(r'[，,。；;！？!?\n]',prefix)[-1]
        if re.search(r'不是|并非|没有|如果|假如|假设',clause_prefix):continue
        if re.match(r'\s*(?:[-–~到至]|或者|或)\s*[\d一二两三四五六七八九十]',suffix):continue
        if re.match(r'\s*(?:人|页|周|天|个月|%|％)',suffix):continue
        if re.match(r'\s*(?:吗|么|是否)',suffix):continue
        value=_money(match[1])
        if value is not None:
            found.append((match.end(),_budget_value(value,match.group(0))))
    # A same-clause explicit correction supersedes its earlier number.
    correction=re.finditer(r'预算\s*(?:从|由)\s*('+AMOUNT+r')\s*(?:改为|改成|调整为|调整到|降到|提高到)\s*('+AMOUNT+r')',text,re.I)
    for match in correction:
        if re.search(r'不是|并非|如果|假如|假设',text[max(0,match.start()-8):match.start()]):continue
        value=_money(match[2])
        if value is not None:found.append((match.end(),_budget_value(value,'预算'+match[2])))
    # Ellipsis is allowed only for an established budget and a standalone
    # monetary correction. Counts, timelines, quotations and project switches
    # must not be silently converted into budget updates.
    if previous_budget is not None:
        implicit=re.fullmatch(r'\s*(?:现在|目前|刚确认|刚确定)?\s*(?:'+CHANGE+r')\s*('+AMOUNT+r')\s*(?:了)?[。！!\s]*',text,re.I)
        if implicit and re.search(r'万|千|元|[kKwW]|块',implicit[1]):
            value=_money(implicit[1])
            if value is not None:found.append((implicit.end(),_budget_value(value,'预算'+implicit[1])))
    for match in re.finditer(r'预算\s*(?:目前|现在|还|暂时|先)?\s*(?:没定|未定|不确定|取消|不设)',text):
        if not re.search(r'不是|并非|如果|假如',text[max(0,match.start()-6):match.start()]):
            found.append((match.end(),{'action':'clear'}))
    return max(found,key=lambda item:item[0])[1] if found else None


def _budget_value(value,text):
    # Keep user qualifiers and familiar units in conversational evidence;
    # normalized numeric value is shared with the persisted profile.
    text=re.sub(r'(?<=\d)\s*[wW](?:元)?(?![A-Za-z])','万元',text)
    return {'action':'set','value':value,'text':text.strip()}


def timeline_update(text):
    if re.match(r'\s*(?:(?:那|那么|请问)[，,\s]*)?(?:如果|假如|假设)',text):return None
    found=[]
    for match in TIMELINE.finditer(text):
        prefix=re.split(r'[，,。；;！？!?\n]',text[max(0,match.start()-8):match.start()])[-1]
        if re.search(r'不是|并非|不要|如果|假如|假设',prefix):continue
        period=match[1] or match[2]
        found.append((match.end(),{'action':'set','value':period,'text':'期望'+period}))
    for match in re.finditer(r'(?:上线时间|交付时间|工期|时间)\s*(?:(?:目前|现在|还|也|暂时|先)\s*){0,3}(?:没定|未定|不确定|取消|不设)',text):
        found.append((match.end(),{'action':'clear'}))
    return max(found,key=lambda item:item[0])[1] if found else None


def update_profile_requirements(profile,text):
    result=dict(profile)
    for key,update,label in [('budget',budget_update(text,previous_budget=profile.get('budget')),'预算范围'),('timeline',timeline_update(text),'计划时间')]:
        if not update:continue
        if update['action']=='clear':result.pop(key,None);continue
        value=update['value']
        result[key]={'label':label,'value':value,'display':f'¥{value:,.0f}' if key=='budget' else value,
                     'evidence':text,'source':'用户原话','confirmed':True}
    return result
