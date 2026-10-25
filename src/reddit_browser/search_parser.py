"""解析搜索 HTML：复用首页的 template + DOM 选择器方式，不发送网络请求。"""



PARSE_SEARCH = r'''
const template = document.createElement('template');
template.innerHTML = arguments[0];
const root = template.content; // 不挂载到页面，不执行响应脚本。
const base = 'https://www.reddit.com';
const posts = [];
for (const card of root.querySelectorAll('[data-testid="search-sdui-post"]')) {
    const context = JSON.parse(card.getAttribute('data-faceplate-tracking-context') || '{}');
    const titleLink = card.querySelector('a[data-testid="post-title"]');
    const textLink = card.querySelector('a[data-testid="post-title-text"]');
    const url = new URL(titleLink?.getAttribute('href') || textLink?.getAttribute('href') || '/', base);
    const id = card.getAttribute('data-thingid') || context.post?.id;
    if (!id || url.origin !== base || !url.pathname.includes('/comments/')) continue;
    const numbers = [...card.querySelectorAll('[data-testid="search-counter-row"] faceplate-number')]
        .map(n => {const v=n.getAttribute('number'); return v !== null && v.trim() && Number.isFinite(Number(v)) ? Number(v) : null;});
    posts.push({
        id,
        title: context.post?.title || titleLink?.getAttribute('aria-label') || textLink?.textContent.trim() || null,
        url: url.href,
        subreddit: context.subreddit?.name ? 'r/' + context.subreddit.name : null,
        subreddit_id: context.subreddit?.id || null,
        author: context.profile?.name || null,
        author_id: context.profile?.id || null,
        created_at: card.querySelector('faceplate-timeago[ts]')?.getAttribute('ts') || null,
        score: numbers[0] ?? null,
        comment_count: numbers[1] ?? null,
        thumbnail_url: card.querySelector('[data-testid="search_post_thumbnail"]')?.getAttribute('src') || null,
    });
}
const next = [...root.querySelectorAll('faceplate-partial[src]')].map(n => ({
    url: new URL(n.getAttribute('src'), base).href,
    method: (n.getAttribute('method') || 'GET').toUpperCase(),
})).filter(n => {
    const u = new URL(n.url);
    return u.origin === base && u.pathname === '/svc/shreddit/search/' &&
        u.searchParams.has('cursor') && n.method === 'GET';
});
return {
    posts: [...new Map(posts.map(p => [p.id, p])).values()],
    next_requests: [...new Map(next.map(n => [n.url, n])).values()],
    empty_message: [...root.querySelectorAll('h1,h2,h3,p')].map(n => n.textContent.trim())
        .find(t => /no results|couldn.t find|没有.*结果|未找到.*结果/i.test(t)) || null,
};
'''


def parse_search(tab, html):
    """只借用浏览器的 DOM 解析能力，HTML 作为参数传入。"""
    return tab.run_js(PARSE_SEARCH, html, timeout=15)
