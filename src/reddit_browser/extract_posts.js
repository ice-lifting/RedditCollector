// 只读查询：标题通过参数传入，不拼接为可执行代码。
const query = arguments[0];
const mode = arguments[1];
const normalize = s => (s || '').trim().replace(/\s+/g, ' ').toLowerCase();
const absolute = value => value ? new URL(value, location.href).href : null;
const attr = (node, name) => node?.getAttribute(name) ?? null;
const cards = [...document.querySelectorAll(['export', 'single_export'].includes(mode)
    ? 'main#main-content shreddit-feed shreddit-post' : 'shreddit-post')];
const matches = ['export', 'detail_export'].includes(mode) ? cards :
    cards.filter(p => normalize(attr(p, 'post-title')) === normalize(query));

function bestPreview(img) {
    if (!img) return null;
    const candidates = (attr(img, 'srcset') || attr(img, 'data-lazy-srcset') ||
        attr(img, 'data-srcset') || '').split(',').map(part => {
        const m = part.trim().match(/^(\S+)\s+(\d+)w$/);
        return m ? {url: m[1], width: Number(m[2])} : null;
    }).filter(Boolean).sort((a, b) => b.width - a.width);
    return absolute(candidates[0]?.url || attr(img, 'src') || attr(img, 'data-lazy-src') ||
        attr(img, 'data-src'));
}

function imageItem(root, position) {
    const preview = root.querySelector('figure > img, img[data-post-media-primary], img#post-image, img.media-lightbox-img');
    const original = root.querySelector('zoomable-img img');
    return {
        position,
        media_type: 'image',
        original_url: absolute(attr(original, 'src') || attr(original, 'data-src')),
        preview_url: bestPreview(preview),
        alt_text: attr(preview, 'alt'),
        preview_policy: 'largest_srcset_candidate',
    };
}

return {
    page_url: location.href,
    loaded_card_count: cards.length,
    matches: matches.map(p => {
        const result = {id: p.id, title: attr(p, 'post-title'),
            post_type: attr(p, 'post-type'), url: absolute(attr(p, 'permalink')),
            subreddit: attr(p, 'subreddit-prefixed-name'), author: attr(p, 'author'),
            content_url: absolute(attr(p, 'content-href'))};
        if (mode === 'basic') return result;
        const body = p.querySelector('shreddit-post-text-body [property="schema:articleBody"]') ||
            p.querySelector('[property="schema:articleBody"]');
        result.body_info = {found: !!body, source: 'currently_present_dom_only',
            character_count: body?.textContent.trim().length || 0,
            visually_clamped: !!body && [...body.classList].some(c => c.includes('line-clamp')),
            completeness: 'not_verified_against_detail'};
        if (mode === 'body') {
            result.body = body?.textContent.trim() ?? null;
            return result;
        }
        if (['export', 'single_export', 'detail_export'].includes(mode)) {
            result.body = body?.textContent.trim() ?? null;
            delete result.body_info;
            result.author_id = attr(p, 'author-id');
            result.subreddit_id = attr(p, 'subreddit-id');
            result.created_at = attr(p, 'created-timestamp');
            for (const [field, name] of Object.entries({score: 'score',
                comment_count: 'comment-count', upvote_ratio: 'upvote-ratio'})) {
                const value = attr(p, name);
                result[field] = value !== null && value.trim() !== '' &&
                    Number.isFinite(Number(value)) ? Number(value) : null;
            }
            result.parse_status = ['image', 'gallery', 'video', 'text', 'crosspost']
                .includes(result.post_type) ? 'parsed' : 'unsupported_type';
            if (result.parse_status === 'unsupported_type') {
                result.media = [];
                return result;
            }
        }
        const container = p.querySelector('[slot="post-media-container"]') || p;
        const gallery = container.querySelector('gallery-carousel');
        const player = container.querySelector('shreddit-player');
        result.media = [];
        if (gallery) {
            result.media = [...gallery.querySelectorAll('li[slot^="page-"]')].map((li, i) => {
                const position = Number(attr(li, 'slot').slice(5)) - 1;
                return imageItem(li, Number.isFinite(position) ? position : i);
            });
        } else if (player) {
            result.media = [{position: 0, media_type: 'video',
                stream_url: absolute(attr(player, 'src')),
                poster_url: absolute(attr(player, 'poster')),
                preview_video_url: absolute(attr(player, 'preview')),
                caption_url: absolute(attr(player, 'caption-url')),
                download_policy: 'poster_only'}];
        } else {
            const image = imageItem(container, 0);
            if (!image.original_url && result.post_type === 'image') {
                image.original_url = result.content_url;
            }
            if (image.original_url || image.preview_url) result.media.push(image);
        }
        result.media_scope = 'currently_present_dom_only';
        if (result.post_type === 'crosspost') {
            result.original_post_url = result.content_url;
            const id = result.content_url?.match(/\/comments\/([a-z0-9]+)\//i)?.[1];
            result.original_post_id = id ? `t3_${id}` : null;
        }
        if (mode === 'structure') {
            result.structure = [...container.querySelectorAll(
                'gallery-carousel, li[slot^="page-"], figure, zoomable-img, img, shreddit-player, source'
            )].slice(0, 30).map(n => ({tag: n.localName, slot: attr(n, 'slot'),
                id: n.id || null, src: attr(n, 'src'),
                data_src: attr(n, 'data-src'), poster: attr(n, 'poster')}));
        }
        return result;
    }),
};
