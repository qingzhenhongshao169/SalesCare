"""One explicit project boundary for all bounded conversation consumers."""
import re

RESTART = re.compile(r'另一个项目|另外一个项目|换个项目|换一个项目|新项目|重新开始')


def restart_position(text):
    if re.match(r'\s*(?:(?:那|那么|请问)[，,\s]*)?(?:如果|假如|假设)',text):return None
    position=None
    for match in RESTART.finditer(text):
        before=re.split(r'[，,。；;！？!?\n]',text[:match.start()])[-1]
        if re.search(r'如果|假如|假设|不是|不要|不用|不想|不需要|别|不换|不重新|你们|贵公司|有没有|是否|能否',before):continue
        after=re.split(r'[，,。；;！？!?\n]',text[match.end():])[0]
        if re.search(r'吗|么|会怎样|是什么意思|怎么操作',after):continue
        position=match.start()
    return position


def project_text(text):
    position=restart_position(text)
    return text[position:] if position is not None else text


def project_history(history):
    start=0
    result=list(history)
    for i,message in enumerate(history):
        if message.get('role') not in ('user','visitor'):continue
        key='content' if 'content' in message else 'text'
        text=message.get(key,'');position=restart_position(text)
        if position is not None:
            start=i;result[i]={**message,key:text[position:]}
    return result[start:]
