"""Plain-data reference jobs; no Tk, controller or automatic cloud requests."""
from dataclasses import dataclass, field
from copy import deepcopy
import io
import re

ANCHORS = {'В центре': 'center', 'Сверху слева': 'top_left', 'Сверху справа': 'top_right',
           'Снизу слева': 'bottom_left', 'Снизу справа': 'bottom_right'}


def reference_defaults(prompt):
    """Keep explicit drawing dimensions separate from the search query."""
    from editor_tools.planner import _CORNERS
    text = prompt.casefold()
    anchor = next((value for corner, value in _CORNERS.items()
                   if re.search(r'\b(?:в|на)\s+' + corner + r'\s+углу\b', text)), 'center')
    width = re.search(r'\bширин(?:ой|а)\s+(\d+(?:[.,]\d+)?)\s*мм\b', text)
    return (width[1].replace(',', '.') if width else '150',
            next(label for label, value in ANCHORS.items() if value == anchor))


def reference_request(prompt):
    """Recognize a request for an actual image, not a geometric sketch edit."""
    if not isinstance(prompt, str):
        return None
    text = ' '.join(prompt.strip().split())
    if not 2 <= len(text) <= 600:
        return None
    if re.search(r'\b(?:напиши|напечатай|добавь|дорисуй|затем|потом)\b', text, re.I):
        return None
    from .reference_images import web_reference_request, request_query, _CAR, _NAMED_LOGO
    request = web_reference_request(text)
    if request is not None and request.query:
        return request.query
    if re.match(r'^(?:нарисуй|изобрази)\b', text, re.I) and _NAMED_LOGO.search(text):
        return request_query(text)
    finding = re.match(r'^(?:найди|поищи|отыщи|загугли|погугли)\s+(.+)', text, re.I)
    if finding and (_CAR.search(finding[1]) or re.search(r'лого|фото|изображени|картинк|референс', finding[1], re.I)):
        return request_query('нарисуй ' + finding[1])
    return None


@dataclass(frozen=True)
class SearchHit:
    candidate: object
    thumbnail: bytes = b''


def selected_plan(result, scene=None, selected=None):
    """Replace only the selected scene object, keeping canonical editor Plans."""
    if selected is None:
        return result.plan
    from editor_tools.scene import scene_plan, checked_scene
    from editor_tools.planner import transform_paths
    w, h = result.sheet
    scene, _ = checked_scene(deepcopy(scene), w, h)
    item = next((item for item in scene['objects'] if item['id'] == selected), None)
    if item is None:
        raise ValueError('Выбранный объект больше не существует.')
    # A trace's scene route can reorder AND reverse its source paths. Flatten
    # the checked rendered plan, not its unrendered first source object.
    # Otherwise the proposal differs from the preview (including pen-down joins).
    rendered = transform_paths([], result.plan, w, h)
    if tuple(tuple(path) for path in rendered) != tuple(tuple(path) for path in result.paths):
        raise ValueError('Контуры образца не совпадают с проверенным предложением. Постройте вариант заново.')
    source = result.plan.parameters['scene']['objects'][0]
    item['name'] = source['name']
    item['source'] = {'kind': 'paths', 'parameters': {'paths': rendered}}
    item['matrix'] = [1, 0, 0, 1, 0, 0]
    if scene['route'] is not None:
        # Preserve the order/directions of every neighboring path. Insert the
        # complete new object at the first occurrence of the replaced object.
        route, inserted = [], False
        for entry in scene['route']:
            if entry['object'] != selected:
                route.append(entry)
            elif not inserted:
                route.extend({'object': selected, 'path': index, 'reverse': False}
                             for index in range(len(rendered)))
                inserted = True
        scene['route'] = route
    checked_scene(scene, w, h)
    return scene_plan(scene, result.plan.explanation + ' Заменяется выбранный объект.')


def _thumbnail(raw):
    from PIL import Image
    from .image_trace import decode_rgb
    image = Image.fromarray(decode_rgb(raw))
    image.thumbnail((220, 160), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def search_references(query, use_sites=False, cancel=None):
    from .reference_images import Candidate, check_cancel, search, download
    hits, seen = [], set()
    # Site lookup sends the query alone. No local files or images are attached.
    if use_sites:
        if re.search(r'\b(?:мфти|mipt)\b', query, re.I) and re.search(r'лого', query, re.I):
            from .reference_images import official_mipt_logo
            try:
                candidate, _ = official_mipt_logo(cancel)
                return (SearchHit(candidate),)
            except (ValueError, OSError):
                check_cancel(cancel)
        from .web_lookup import lookup
        from .web_assets import image_links, download_image
        try:
            rows = lookup(query, cancel=cancel)
        except (ValueError, OSError):
            check_cancel(cancel)
            rows = []
        for row in rows:
            check_cancel(cancel)
            try:
                urls = [row['asset']] if row.get('asset') else image_links(row['page'], query, cancel=cancel)
                for url in urls[:2]:
                    if url in seen:
                        continue
                    raw, mime = download_image(url, cancel=cancel)
                    candidate = Candidate(row['title'], url, url, row['page'], mime,
                                          license=row['note'], origin='website')
                    thumb = b'' if mime == 'image/svg+xml' else _thumbnail(raw)
                    hits.append(SearchHit(candidate, thumb))
                    seen.add(url)
                    break
            except (ValueError, OSError):
                check_cancel(cancel)
            if len(hits) >= 5:
                break
    if not hits:
        for candidate in search(query, cancel=cancel, limit=6):
            check_cancel(cancel)
            thumb = b''
            try:
                thumb = _thumbnail(download(candidate.thumbnail, cancel=cancel, limit=1_500_000))
            except (ValueError, OSError):
                check_cancel(cancel)
            hits.append(SearchHit(candidate, thumb))
    return tuple(hits)


def load_reference(candidate, cancel=None):
    from .reference_images import check_cancel, download
    from .image_trace import asset_from_bytes
    check_cancel(cancel)
    if candidate.origin == 'website':
        from .web_assets import download_image
        raw, mime = download_image(candidate.url, cancel=cancel)
    else:
        raw, mime = download(candidate.url, cancel=cancel, official=candidate.origin in ('official', 'mipt')), candidate.mime
        if candidate.origin == 'mipt':
            from .reference_images import first_mipt_frame
            raw = first_mipt_frame(raw)
    check_cancel(cancel)
    return asset_from_bytes(raw, candidate.title, mime=mime)


@dataclass
class ReviewBudget:
    """Two image reviews per local build, cached by the exact image and paths."""
    calls: int = 0
    cache: dict = field(default_factory=dict)

    def review(self, result, cancel=None):
        from .trace_review import fingerprint, review
        key = fingerprint(result)
        if key in self.cache:
            return self.cache[key]
        if self.calls >= 2:
            raise ValueError('Для этого набора уже выполнены две проверки. Постройте новый набор при необходимости.')
        self.calls += 1
        report = review(result, cancel=cancel)
        self.cache[key] = report
        return report


def run_job(events, token, cancel, kind, payload):
    """Worker target has only queue, event and immutable/plain data arguments."""
    try:
        from .reference_images import check_cancel
        check_cancel(cancel)
        if kind == 'search':
            value = search_references(*payload, cancel=cancel)
        elif kind == 'file':
            from .image_trace import read_asset
            value = read_asset(payload)
        elif kind == 'download':
            value = load_reference(payload, cancel)
        elif kind == 'build':
            from .art_direction import build_variants
            value = build_variants(*payload, cancel=cancel)
        elif kind == 'choose':
            from .art_direction import choose_variants
            value = choose_variants(*payload, cancel=cancel)
        elif kind == 'review':
            budget, result = payload
            value = budget.review(result, cancel)
        elif kind == 'repair':
            from .trace_review import repair
            value = repair(*payload, cancel=cancel)
        else:
            raise ValueError('Неизвестная операция с образцом.')
        check_cancel(cancel)
        events.put((token, kind, value))
    except Exception as exc:
        if not cancel.is_set():
            events.put((token, 'error', str(exc)))
