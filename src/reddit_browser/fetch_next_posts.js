// 单次请求：使用页面分页地址或上一批的地址，不更新真实 DOM。
const inspectOnly = arguments[0];
const timeoutMs = arguments[1];
const suppliedUrl = arguments[2];
return (async () => {
    if (location.hostname !== 'www.reddit.com' || location.pathname !== '/') {
        return {status:'stopped', reason:'not_home', request_sent:false};
    }
    const state = () => ({url:location.href, scroll_y:scrollY,
        post_ids:[...document.querySelectorAll('main#main-content shreddit-feed shreddit-post')].map(p=>p.id)});
    const before = state();
    const feed = document.querySelector('main#main-content shreddit-feed');
    if (!feed) return {status:'stopped',reason:'feed_not_found',request_sent:false};
    const candidates = suppliedUrl ? [{url:suppliedUrl, method:'GET'}] : [...feed.querySelectorAll('faceplate-partial[src]')].map(n => {
        const url = new URL(n.getAttribute('src'), location.href);
        return {url:url.href, method:(n.getAttribute('method') || 'GET').toUpperCase(),
            loading:n.getAttribute('loading'), id:n.id};
    });
    const validCandidates = candidates.filter(n => {
        const url = new URL(n.url);
        return url.origin === location.origin && !url.username && !url.password && !url.hash &&
            url.pathname === '/svc/shreddit/feeds/home-feed' &&
            url.searchParams.getAll('cursor').length === 1 && !!url.searchParams.get('cursor') &&
            url.searchParams.getAll('after').length === 1 && !!url.searchParams.get('after') && n.method === 'GET';
    });
    const unique = [...new Map(validCandidates.map(n=>[n.url,n])).values()];
    if (unique.length !== 1) return {status:'stopped',reason:unique.length?'ambiguous_next_url':'next_url_not_found',
        candidates:unique, before, request_sent:false};
    const request = unique[0];
    if (inspectOnly) return {status:'inspected',request,before,request_sent:false};
    const controller = new AbortController();
    const timer = setTimeout(()=>controller.abort(),timeoutMs);
    const start = performance.now();
    try {
        const response = await fetch(request.url, {method:'GET', credentials:'same-origin',
            redirect:'error', signal:controller.signal});
        const contentType = response.headers.get('content-type') || '';
        const metadata = {status_code:response.status, response_url:response.url,
            content_type:contentType, request, before, after:state(), request_sent:true};
        if (!response.ok) return {...metadata,status:'stopped',reason:'http_error'};
        if (!contentType.toLowerCase().includes('html')) {
            return {...metadata,status:'stopped',reason:'unexpected_content_type'};
        }
        const html = await response.text();
        if (html.length > 5*1024*1024) return {...metadata,status:'stopped',reason:'response_too_large'};
        return {...metadata,after:state(),status:'fetched',html,
            html_characters:html.length,elapsed_ms:Math.round(performance.now()-start)};
    } catch (error) {
        return {status:'stopped',reason:'fetch_failed',message:String(error),request,
            before,after:state(),request_sent:true};
    } finally { clearTimeout(timer); }
})();
