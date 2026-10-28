#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "selenium",
#     "beautifulsoup4",
#     "lxml",
#     "trafilatura",
#     "readability-lxml",
#     "markdownify",
#     "markdown",
#     "ebooklib",
#     "requests",
#     "tinycss2",
# ]
# ///
"""Download a Zenn book as markdown files and convert them into an EPUB file."""

import argparse
import hashlib
import json
import logging
import mimetypes
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import markdown
import requests
import tinycss2
import trafilatura
from bs4 import BeautifulSoup
from ebooklib import epub
from markdownify import markdownify
from readability import Document
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

MIN_CONTENT_LENGTH = 100
MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 3.0
BOOK_URL_PATTERN = re.compile(r'^https://zenn\.dev/[^/]+/books/[^/]+$')
METADATA_FILE = 'book.json'
STYLE_FILE = 'style.css'
CONTENT_CLASS = 'znc'


def wait_before_request(max_delay: float = MAX_DELAY_SECONDS) -> None:
    """Wait for a random duration before making a request."""
    delay = random.uniform(MIN_DELAY_SECONDS, max_delay)
    logger.debug(f"Waiting {delay:.2f} seconds before request...")
    time.sleep(delay)


def normalize_url(url: str) -> str:
    """Normalize a URL (removes query and fragment parts)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def create_driver() -> webdriver.Chrome:
    """Create and configure headless Chrome WebDriver."""
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    return webdriver.Chrome(options=options)


def fetch_html_content(url: str, driver: webdriver.Chrome) -> str:
    """Fetch rendered HTML content from a URL using Selenium."""
    wait_before_request()
    driver.get(url)
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    time.sleep(2)
    return driver.page_source


def extract_page_title(html_content: str) -> str:
    """Extract a page title from HTML content, preferring <h1> then <title>."""
    soup = BeautifulSoup(html_content, 'html.parser')
    h1 = soup.find('h1')
    if h1:
        return h1.get_text(strip=True)
    title_tag = soup.find('title')
    if title_tag:
        return title_tag.get_text(strip=True)
    return "Untitled"


def extract_chapter_urls(book_url: str, html_content: str) -> list[str]:
    """Extract chapter URLs in DOM order from the book top page."""
    soup = BeautifulSoup(html_content, 'html.parser')
    prefix = book_url.rstrip('/') + '/viewer/'
    urls = {}
    for anchor in soup.find_all('a', href=True):
        url = normalize_url(urljoin(book_url, anchor['href']))
        if url.startswith(prefix) and '/' not in url[len(prefix):]:
            urls[url] = None
    return list(urls)


def extract_by_xpath(html_content: str) -> Optional[str]:
    """Extract main content using common CSS selectors (Zenn's .znc first)."""
    soup = BeautifulSoup(html_content, 'html.parser')
    for selector in ['.znc', 'article', 'main', '[role="main"]', '#content', '.content']:
        element = soup.select_one(selector)
        if element:
            return str(element)
    return None


def extract_by_trafilatura(html_content: str) -> Optional[str]:
    """Extract main content as HTML using trafilatura."""
    try:
        return trafilatura.extract(html_content, include_images=True, output_format='html')
    except Exception as e:
        logger.debug(f"Trafilatura extraction error: {e}")
    return None


def extract_by_readability(html_content: str) -> Optional[str]:
    """Extract main content using readability-lxml."""
    try:
        return Document(html_content).summary()
    except Exception as e:
        logger.debug(f"Readability extraction error: {e}")
    return None


def extract_by_body(html_content: str) -> Optional[str]:
    """Return <body> element HTML as final fallback."""
    body = BeautifulSoup(html_content, 'html.parser').find('body')
    return str(body) if body else None


def extract_main_content(html_content: str) -> str:
    """Extract main content HTML with fallback (xpath > trafilatura > readability > body)."""
    extractors = {
        "xpath": extract_by_xpath,
        "trafilatura": extract_by_trafilatura,
        "readability": extract_by_readability,
        "body": extract_by_body,
    }
    for name, extractor in extractors.items():
        result = extractor(html_content)
        if result and len(BeautifulSoup(result, 'html.parser').get_text(strip=True)) >= MIN_CONTENT_LENGTH:
            logger.debug(f"Content extracted successfully using: {name}")
            return result
        logger.debug(f"{name} result too short or empty")
    return html_content


def html_to_markdown(content_html: str, page_url: str) -> str:
    """Convert extracted HTML to markdown with absolute image/link URLs."""
    soup = BeautifulSoup(content_html, 'html.parser')
    for button in soup.find_all('button'):
        button.decompose()
    for img in soup.find_all('img'):
        src = img.get('data-src') or img.get('src')
        if src and not src.startswith('data:'):
            img['src'] = urljoin(page_url, src)
    for anchor in soup.find_all('a', href=True):
        anchor['href'] = urljoin(page_url, anchor['href'])
    return markdownify(str(soup), heading_style='ATX').strip() + '\n'


def download_book(book_url: str, book_dir: Path, refetch: bool) -> None:
    """Download the book's chapters as markdown files and save metadata."""
    driver = create_driver()
    try:
        logger.info(f"Fetching book: {book_url}")
        top_html = fetch_html_content(book_url, driver)
        chapter_urls = extract_chapter_urls(book_url, top_html)
        if not chapter_urls:
            raise RuntimeError("No chapters found")
        metadata = {
            'url': book_url,
            'title': extract_page_title(top_html),
            'author': urlparse(book_url).path.strip('/').split('/')[0],
            'chapters': [],
        }
        logger.info(f"Title: {metadata['title']} ({len(chapter_urls)} chapters)")

        book_dir.mkdir(parents=True, exist_ok=True)
        metadata['cover'] = download_cover(book_url, top_html, book_dir)
        if refetch or not (book_dir / STYLE_FILE).exists():
            download_style(book_url, top_html, book_dir)
        for i, url in enumerate(chapter_urls, 1):
            filepath = book_dir / f"{i:03}_{url.split('/')[-1]}.md"
            if filepath.exists() and not refetch:
                logger.info(f"[{i}/{len(chapter_urls)}] Skip (already exists): {filepath}")
                title = filepath.read_text(encoding='utf-8').splitlines()[0].lstrip('# ').strip()
            else:
                logger.info(f"[{i}/{len(chapter_urls)}] {url}")
                html = fetch_html_content(url, driver)
                title = extract_page_title(html)
                body = html_to_markdown(extract_main_content(html), url)
                filepath.write_text(f"# {title}\n\n{body}", encoding='utf-8')
            metadata['chapters'].append({'file': filepath.name, 'title': title, 'url': url})
    finally:
        driver.quit()

    (book_dir / METADATA_FILE).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')


def fetch_resource(url: str) -> Optional[requests.Response]:
    """Fetch a resource, logging request failures and returning None."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        logger.warning(f"Failed to fetch resource: {url} - {e}")
        return None


def download_cover(book_url: str, html_content: str, book_dir: Path) -> Optional[str]:
    """Download the book cover image from the top page and return its file name."""
    img = BeautifulSoup(html_content, 'html.parser').select_one('#__next article section header img')
    src = img and (img.get('data-src') or img.get('src'))
    if not src:
        logger.warning("Cover image not found")
        return None
    response = fetch_resource(urljoin(book_url, src))
    if response is None:
        return None
    media_type = response.headers.get('Content-Type', '').split(';')[0] or 'image/png'
    name = f"cover{mimetypes.guess_extension(media_type) or '.png'}"
    (book_dir / name).write_bytes(response.content)
    logger.info(f"Cover saved: {book_dir / name}")
    return name


def filter_content_rules(rules: list) -> list:
    """Keep only CSS rules targeting the Zenn content class (recursing into at-rules)."""
    kept = []
    for rule in rules:
        if rule.type == 'qualified-rule' and f'.{CONTENT_CLASS}' in tinycss2.serialize(rule.prelude):
            kept.append(rule)
        elif rule.type == 'at-rule' and rule.content is not None and rule.lower_at_keyword in ('media', 'supports'):
            children = filter_content_rules(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True))
            if children:
                rule.content = children
                kept.append(rule)
    return kept


def download_style(book_url: str, html_content: str, book_dir: Path) -> None:
    """Download Zenn stylesheets and save the content-related rules as style.css."""
    soup = BeautifulSoup(html_content, 'html.parser')
    hrefs = dict.fromkeys(urljoin(book_url, link['href']) for link in soup.select('link[rel~=stylesheet][href]'))
    rules = []
    for href in hrefs:
        response = fetch_resource(href)
        if response is None:
            continue
        rules += filter_content_rules(tinycss2.parse_stylesheet(response.text, skip_comments=True, skip_whitespace=True))
    if not rules:
        logger.warning("No content styles found")
        return
    (book_dir / STYLE_FILE).write_text('\n'.join(rule.serialize() for rule in rules) + '\n', encoding='utf-8')
    logger.info(f"Style saved: {book_dir / STYLE_FILE} ({len(rules)} rules)")


def embed_images(content_html: str, book: epub.EpubBook, cache: dict[str, str]) -> str:
    """Download images, add them to the book, and rewrite their src attributes."""
    soup = BeautifulSoup(content_html, 'html.parser')
    for img in soup.find_all('img'):
        src = img.get('src')
        if not src or not src.startswith('http'):
            continue
        if src not in cache:
            response = fetch_resource(src)
            if response is None:
                continue
            media_type = response.headers.get('Content-Type', '').split(';')[0] or 'image/png'
            ext = mimetypes.guess_extension(media_type) or '.png'
            name = f"images/{hashlib.sha1(src.encode()).hexdigest()}{ext}"
            book.add_item(epub.EpubItem(uid=name, file_name=name, media_type=media_type, content=response.content))
            cache[src] = name
        img['src'] = cache[src]
    return str(soup)


def build_epub(book_dir: Path, output: Path) -> None:
    """Build an EPUB file from downloaded markdown files."""
    metadata = json.loads((book_dir / METADATA_FILE).read_text(encoding='utf-8'))

    book = epub.EpubBook()
    book.set_identifier(f"zenn-{book_dir.name}")
    book.set_title(metadata['title'])
    book.set_language('ja')
    book.add_author(metadata['author'])
    if metadata.get('cover'):
        cover = metadata['cover']
        book.set_cover(cover, (book_dir / cover).read_bytes())

    style = None
    if (book_dir / STYLE_FILE).exists():
        style = epub.EpubItem(uid='style', file_name=f"style/{STYLE_FILE}", media_type='text/css',
                              content=(book_dir / STYLE_FILE).read_bytes())
        book.add_item(style)

    chapters = []
    image_cache: dict[str, str] = {}
    for i, entry in enumerate(metadata['chapters'], 1):
        text = (book_dir / entry['file']).read_text(encoding='utf-8')
        html = markdown.markdown(text, extensions=['fenced_code', 'tables'])
        chapter = epub.EpubHtml(title=entry['title'], file_name=f"chapter_{i:03}.xhtml", lang='ja')
        chapter.content = f'<div class="{CONTENT_CLASS}">{embed_images(html, book, image_cache)}</div>'
        if style:
            chapter.add_item(style)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['nav', *chapters]
    epub.write_epub(str(output), book)
    logger.info(f"Saved: {output}")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description='Download a Zenn book as markdown and convert it into an EPUB file.')
    parser.add_argument('url', help='Zenn book URL (e.g., https://zenn.dev/<user>/books/<id>)')
    parser.add_argument('-d', '--dir', default='books', metavar='DIR', help='Directory to save markdown per book (default: books)')
    parser.add_argument('-o', '--output', metavar='FILE', help='Output EPUB path (default: <DIR>/<book_id>.epub)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-r', '--refetch', action='store_true', help='Re-download all chapters, overwriting existing markdown')
    group.add_argument('-l', '--local', action='store_true', help='Build EPUB from downloaded markdown only (no network access to Zenn)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Show verbose (DEBUG level) logs')
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s: %(message)s')

    book_url = normalize_url(args.url)
    if not BOOK_URL_PATTERN.match(book_url):
        logger.error("Please provide a Zenn book URL (e.g., https://zenn.dev/<user>/books/<id>)")
        sys.exit(1)

    book_id = book_url.split('/')[-1]
    book_dir = Path(args.dir) / book_id
    output = Path(args.output or Path(args.dir) / f"{book_id}.epub")
    try:
        if args.local:
            if not (book_dir / METADATA_FILE).exists():
                logger.error(f"Downloaded book not found: {book_dir}")
                sys.exit(1)
        else:
            download_book(book_url, book_dir, args.refetch)
        build_epub(book_dir, output)
    except KeyboardInterrupt:
        logger.error("Interrupted")
        sys.exit(1)


if __name__ == "__main__":
    main()
