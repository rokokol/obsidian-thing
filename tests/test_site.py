import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
SITE = Path(os.environ.get('OBSIDIAN_THING_SITE', REPO / 'site')).resolve()
SERVER = Path(os.environ.get('OBSIDIAN_THING_SERVER', REPO / 'src' / 'obsidian_thing' / 'server.py'))
DOMAIN = 'obsidian-thing.rokokol.art'
# The cover card is a rendering surface, not a site page: it has its own size, no navigation
# and a noindex, and the site test leaves it to the cover test rather than teaching it to be
# a page
RENDERING = {SITE / 'assets' / 'cover.html'}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.text: list[str] = []
        self.children: list['Node'] = []

    @property
    def content(self) -> str:
        return ''.join(self.text)


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('document', {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        values = {name: value or '' for name, value in attrs}
        node = Node(tag, values)
        self.stack[-1].children.append(node)
        if tag not in ('meta', 'link', 'br', 'img', 'input', 'hr', 'source', 'path'):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        for node in self.stack[1:]:
            node.text.append(data)


def parse(path: Path) -> Page:
    page = Page()
    page.feed(path.read_text(encoding='utf-8'))
    page.close()
    return page


def walk(node: Node):
    for child in node.children:
        yield child
        yield from walk(child)


def pages() -> list[Path]:
    return sorted(path for path in SITE.rglob('*.html')
                  if path.parent.name != 'vendor' and path not in RENDERING)


def relative(path: Path) -> str:
    return str(path.relative_to(SITE))


def test_the_site_has_the_pages_the_readme_names():
    assert {relative(page) for page in pages()} == {
        'index.html', 'features.html', 'tools.html', 'faq.html', '404.html',
        'docs/index.html', 'docs/connect.html', 'docs/safety.html', 'docs/search.html',
    }


def test_every_internal_reference_resolves_to_a_file():
    missing = []
    for page in pages():
        for node in walk(parse(page).root):
            for name in ('href', 'src'):
                value = node.attrs.get(name)
                if not value or value.startswith(('#', 'mailto:', 'http://', 'https://', 'data:')):
                    continue
                path = urlsplit(value).path
                if not path:
                    continue
                resolved = SITE / path.lstrip('/') if path.startswith('/') else (page.parent / path)
                if not resolved.resolve().is_file():
                    missing.append(f'{relative(page)}: {value}')
    assert missing == []


def test_every_page_links_the_same_four_stylesheets_and_two_scripts():
    signatures = {}
    for page in pages():
        nodes = list(walk(parse(page).root))
        sheets = [Path(node.attrs['href']).name for node in nodes
                  if node.tag == 'link' and node.attrs.get('rel') == 'stylesheet']
        scripts = [Path(node.attrs['src']).name for node in nodes
                   if node.tag == 'script' and node.attrs.get('src')]
        # A page inside docs/ climbs one level and 404.html is root-absolute, so the base
        # names are compared and the link test above proves that each spelling resolves
        signatures[relative(page)] = (tuple(sheets), tuple(scripts))
    assert len(set(signatures.values())) == 1, signatures


def test_the_navigation_is_the_same_on_every_page():
    menus = {}
    for page in pages():
        links = []
        for node in walk(parse(page).root):
            if node.tag == 'nav' and 'tabs' in node.attrs.get('class', ''):
                links.append(tuple(Path(link.attrs.get('href', '')).name
                                   for link in node.children if link.tag == 'a'))
        menus[relative(page)] = tuple(links)
    assert len(set(menus.values())) == 1, menus
    assert next(iter(menus.values())) == ((
        'index.html', 'features.html', 'tools.html', 'index.html', 'faq.html',
    ),)


def test_every_navigation_marks_exactly_one_current_link_and_every_page_has_one_h1():
    # Each navigation carries one current link: the header for a top-level page, and the
    # sidebar of the guide for a page inside docs/. A 404 marks none, because no page of the
    # site is the one that was asked for
    for page in pages():
        nodes = list(walk(parse(page).root))
        navigations = [node for node in nodes
                       if node.tag == 'nav'
                       or (node.tag == 'aside' and 'doc-nav' in node.attrs.get('class', ''))]
        assert navigations, relative(page)
        for navigation in navigations:
            current = [link.attrs['href'] for link in walk(navigation)
                       if link.tag == 'a' and link.attrs.get('aria-current') == 'page']
            expected = 0 if page.name == '404.html' and navigation.tag == 'nav' else 1
            assert len(current) == expected, (relative(page), navigation.tag, current)
        assert sum(node.tag == 'h1' for node in nodes) == 1, relative(page)


def test_every_page_carries_both_languages_for_its_title_and_description():
    for page in pages():
        nodes = list(walk(parse(page).root))
        titles = [node for node in nodes if node.tag == 'title']
        assert len(titles) == 1 and titles[0].attrs.get('data-ru'), relative(page)
        descriptions = [node for node in nodes
                        if node.tag == 'meta' and node.attrs.get('name') == 'description']
        assert len(descriptions) == 1, relative(page)
        assert descriptions[0].attrs.get('data-ru-content'), relative(page)
        assert descriptions[0].attrs.get('content'), relative(page)


def test_every_page_carries_the_language_switch_and_the_theme_button():
    for page in pages():
        nodes = list(walk(parse(page).root))
        buttons = [node for node in nodes if node.tag == 'button']
        assert any(node.attrs.get('data-lang-set') == 'en' for node in buttons), relative(page)
        assert any(node.attrs.get('data-lang-set') == 'ru' for node in buttons), relative(page)
        assert any(node.attrs.get('id') == 'theme' for node in buttons), relative(page)


def test_no_page_carries_a_markdown_backtick_or_a_fixed_pixel_size():
    for page in pages():
        text = page.read_text(encoding='utf-8')
        assert '`' not in text, relative(page)
        # A drawn value may set a percentage: the meter of the endpoint card does. A layout
        # measured in pixels would not survive the next face or the next screen
        assert not re.search(r'style="[^"]*:\s*\d+px', text), relative(page)


def test_every_page_carries_the_mark_and_its_favicon():
    # One file is both: the mark on the page is a mask over it, so a page that shows the mark
    # proves the file exists, and the favicon link names the same file
    assert (SITE / 'assets' / 'logo.svg').is_file()
    for page in pages():
        nodes = list(walk(parse(page).root))
        assert any('brand-mark' in node.attrs.get('class', '').split() for node in nodes), relative(page)
        icons = [node.attrs['href'] for node in nodes
                 if node.tag == 'link' and node.attrs.get('rel') == 'icon']
        assert len(icons) == 1 and Path(icons[0]).name == 'logo.svg', relative(page)
        assert not any(node.tag == 'img' and 'logo.svg' in node.attrs.get('src', '') for node in nodes), relative(page)


def test_every_table_has_a_caption_and_scoped_header_cells():
    for page in pages():
        nodes = list(walk(parse(page).root))
        tables = [node for node in nodes if node.tag == 'table']
        cells = [node for node in nodes if node.tag == 'th']
        assert len(tables) == sum(node.tag == 'caption' for node in nodes), relative(page)
        assert all('scope' in node.attrs for node in cells), relative(page)


def test_the_tool_page_names_exactly_the_tools_the_server_registers():
    source = SERVER.read_text(encoding='utf-8')
    registered = set(re.findall(r"name='(obsidian\.[a-z_]+)'", source))
    # One tool is registered through a constant, so its name is read from the constant
    registered |= set(re.findall(r"_MAP_TOOL = '(obsidian\.[a-z_]+)'", source))
    assert len(registered) > 20
    listed = set()
    for node in walk(parse(SITE / 'tools.html').root):
        if node.tag == 'th' and 'tool-name' in node.attrs.get('class', ''):
            listed.update(re.findall(r'obsidian\.[a-z_]+', node.content))
    assert listed - registered == set(), sorted(listed - registered)
    assert registered - listed == set(), sorted(registered - listed)


def test_the_filter_counts_match_the_rows():
    nodes = list(walk(parse(SITE / 'tools.html').root))
    counts = {node.attrs['data-filter']: node.content
              for node in nodes if node.tag == 'button' and 'data-filter' in node.attrs}
    rows = [node.attrs['data-group'] for node in nodes if node.tag == 'tr' and 'data-group' in node.attrs]
    assert counts, 'the tools page has no filter buttons'
    all_button = counts.pop('all')
    assert str(len(rows)) in all_button, all_button
    for group, label in counts.items():
        assert str(rows.count(group)) in label, (group, label, rows.count(group))
    assert sum(rows.count(group) for group in counts) == len(rows)


def test_the_cover_is_the_card_the_metadata_promises():
    header = (SITE / 'assets' / 'cover.png').read_bytes()[:24]
    assert header[:8] == b'\x89PNG\r\n\x1a\n'
    width = int.from_bytes(header[16:20], 'big')
    height = int.from_bytes(header[20:24], 'big')
    assert (width, height) == (1200, 630)
    for page in (SITE / 'index.html', SITE / 'features.html'):
        text = page.read_text(encoding='utf-8')
        assert f'https://{DOMAIN}/assets/cover.png' in text, relative(page)


def test_the_domain_is_one_value():
    assert (SITE / 'CNAME').read_text(encoding='utf-8').strip() == DOMAIN
    for page in pages():
        text = page.read_text(encoding='utf-8')
        for found in re.findall(r'https://([a-z0-9.-]+)/assets/', text):
            assert found == DOMAIN, (relative(page), found)


def test_the_404_page_uses_root_absolute_paths():
    text = (SITE / '404.html').read_text(encoding='utf-8')
    assert 'href="./assets/' not in text
    assert 'href="/assets/' in text


def test_every_page_states_the_licence_and_credits_the_kit():
    for page in pages():
        text = page.read_text(encoding='utf-8')
        assert 'Obsidian is a trademark of Dynalist Inc.' in text, relative(page)
        assert 'github.com/rokokol/ddlc-themes' in text, relative(page)


def test_every_copy_button_carries_one_line_of_command():
    for page in pages():
        for node in walk(parse(page).root):
            if 'data-copy' in node.attrs:
                assert node.attrs['data-copy'].strip(), relative(page)
                assert '\n' not in node.attrs['data-copy'], relative(page)
                assert "'\"'" not in node.attrs['data-copy'], relative(page)


def test_every_language_attribute_has_a_value():
    for page in pages():
        for node in walk(parse(page).root):
            for name, value in node.attrs.items():
                if name == 'data-ru' or name.startswith('data-ru-'):
                    assert value.strip(), (relative(page), node.tag, name)


def test_no_page_loads_anything_from_another_origin():
    for page in pages():
        for node in walk(parse(page).root):
            for name in ('href', 'src'):
                value = node.attrs.get(name, '')
                if value.startswith(('http://', 'https://')) and node.tag in ('link', 'script', 'img'):
                    assert value.startswith('https://github.com/'), (relative(page), value)
