"""Local, deterministic short titles for WeChat Channels only."""
import re
from .titles import normalize_title


SHORT_TITLE_LIMIT = 16


def title_length(value):
    """Match browser string/maxlength counting, including punctuation and emoji."""
    return len(value.encode('utf-16-le')) // 2


def _take(value, budget, *, end=False):
    result=[]
    for char in reversed(value) if end else value:
        cost=title_length(char)
        if cost > budget:
            break
        result.append(char)
        budget-=cost
    return ''.join(reversed(result) if end else result)


def shorten_title(value, limit=SHORT_TITLE_LIMIT):
    """Normalize punctuation, then compress long titles without external services.

    Phrase substitutions retain subjects, negation and numerical expressions.
    If those rules cannot fit a title, retain the topic and final predicate,
    separated by a space. Never introduce unsupported ellipsis punctuation.
    The limit is internal, never an account setting.
    """
    if not 1 <= limit <= SHORT_TITLE_LIMIT:
        raise ValueError('微信视频号短标题内部上限必须在 1–16 之间')
    text=normalize_title(value)
    if title_length(text) <= limit:
        return text
    # Only remove the application's generated asset suffix, not dates or
    # underscores in the actual topic. The source file itself is never renamed.
    text=re.sub(r'_20\d{6}_书声\d+(?:\.\d+)*$', '', text)
    text=re.sub(r'\s+', ' ', text).strip()
    if title_length(text) <= limit:
        return text
    text=re.sub(r'^(?:(?:你知道吗|知识分享)[？?，,：:\s]+|(?:你是否知道|你知道(?!的))[，,：:\s]*)', '', text)
    # Drop an emphatic "就是" only in a negative clause, never the copula in
    # an equality such as "1+1就是2" or "到底" in "坚持到底".
    text=re.sub(r'([，, ])就是(?=.{0,3}(?:不|没|无))', r'\1', text)
    for before,after in (
        ('究竟为什么','为何'),('到底为什么','为何'),('为什么','为何'),
        ('有什么区别','有何区别'),('有什么关系','有何关系'),
        ('是怎么做到的','如何做到'),('是怎么回事','怎么回事'),
        ('滚来滚去','滚动'),('来来回回','往返'),
        ('伸出援手','帮忙'),('出手相助','相助'),
        ('没有办法','无法'),('没有人','无人'),
        ('并不是','并非'),('并不能','不能'),
        ('也就是说','即'),('也正是因为','因为'),('这是因为','因为'),
        ('事实上',''),
    ):
        text=text.replace(before,after)
    text=text.strip(' ，,；;：:')
    if title_length(text) <= limit:
        return text
    text=re.sub(r'[“”「」『』《》]', '', text)
    if title_length(text) <= limit:
        return text
    if limit == 1:
        return _take(text,1)
    head=(limit-1)//2
    tail=limit-1-head
    return normalize_title(_take(text,head)+' '+_take(text,tail,end=True))
