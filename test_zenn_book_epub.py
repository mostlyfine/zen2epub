import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import requests
import tinycss2
from bs4 import BeautifulSoup

import zenn_book_epub as converter


def response(content=b'image', media_type='image/png'):
    result = requests.Response()
    result.status_code = 200
    result._content = content
    result.headers['Content-Type'] = media_type
    return result


class ResourceFetchTests(unittest.TestCase):
    @patch.object(converter.requests, 'get')
    def test_returns_successful_response(self, get):
        get.return_value = response()
        self.assertIs(converter.fetch_resource('https://example.com/image.png'), get.return_value)
        get.assert_called_once_with('https://example.com/image.png', timeout=10)

    @patch.object(converter.requests, 'get')
    def test_returns_none_and_logs_network_or_http_errors(self, get):
        for error in (requests.Timeout('timeout'), requests.HTTPError('404')):
            with self.subTest(error=type(error).__name__):
                get.side_effect = error if isinstance(error, requests.Timeout) else None
                get.return_value = response()
                get.return_value.status_code = 404
                with self.assertLogs(converter.logger, level='WARNING') as logs:
                    self.assertIsNone(converter.fetch_resource('https://example.com/missing'))
                self.assertIn('https://example.com/missing', logs.output[0])


class DownloadTests(unittest.TestCase):
    @patch.object(converter.requests, 'get')
    def test_downloads_cover_with_absolute_url_and_correct_extension(self, get):
        get.return_value = response(b'cover bytes', 'image/jpeg; charset=binary')
        html = '<div id="__next"><article><section><header><img src="/cover.jpg"></header></section></article></div>'
        with tempfile.TemporaryDirectory() as directory:
            book_dir = Path(directory)
            name = converter.download_cover('https://zenn.dev/author/books/book', html, book_dir)
            self.assertEqual(name, 'cover.jpg')
            self.assertEqual((book_dir / name).read_bytes(), b'cover bytes')
        get.assert_called_once_with('https://zenn.dev/cover.jpg', timeout=10)

    @patch.object(converter.requests, 'get', side_effect=requests.Timeout('timeout'))
    def test_cover_failure_leaves_no_file(self, get):
        html = '<div id="__next"><article><section><header><img src="/cover.png"></header></section></article></div>'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs(converter.logger, level='WARNING'):
                self.assertIsNone(converter.download_cover('https://zenn.dev', html, Path(directory)))
            self.assertEqual(list(Path(directory).iterdir()), [])

    @patch.object(converter.requests, 'get')
    def test_style_download_skips_failure_and_duplicate_links(self, get):
        get.side_effect = [requests.Timeout('timeout'), response(b'.znc { color: blue; }', 'text/css')]
        html = '<link rel="stylesheet" href="/failed.css"><link rel="stylesheet" href="/ok.css"><link rel="stylesheet" href="/ok.css">'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs(converter.logger, level='WARNING'):
                converter.download_style('https://zenn.dev', html, Path(directory))
            self.assertEqual((Path(directory) / 'style.css').read_text(), '.znc { color: blue; }\n')
        self.assertEqual(get.call_count, 2)

    @patch.object(converter.requests, 'get')
    def test_image_cache_is_shared_across_chapters(self, get):
        get.return_value = response()
        book = converter.epub.EpubBook()
        cache = {}
        html = '<img src="https://example.com/image.png">'
        first = converter.embed_images(html, book, cache)
        second = converter.embed_images(html, book, cache)
        self.assertEqual(first, second)
        src = BeautifulSoup(first, 'html.parser').img['src']
        items = list(book.get_items())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].file_name, src)
        self.assertEqual(items[0].content, b'image')
        get.assert_called_once_with('https://example.com/image.png', timeout=10)

    @patch.object(converter.requests, 'get', side_effect=requests.Timeout('timeout'))
    def test_image_failure_preserves_original_url(self, get):
        book = converter.epub.EpubBook()
        cache = {}
        html = '<img src="https://example.com/image.png"/>'
        with self.assertLogs(converter.logger, level='WARNING'):
            self.assertEqual(converter.embed_images(html, book, cache), html)
        self.assertEqual(list(book.get_items()), [])
        self.assertEqual(cache, {})


class ChapterUrlTests(unittest.TestCase):
    book_url = 'https://zenn.dev/author/books/example'

    def test_preserves_order_and_removes_duplicate_urls(self):
        html = '''
            <a href="/author/books/example/viewer/second?x=1">Second</a>
            <a href="https://zenn.dev/author/books/example/viewer/first/">First</a>
            <a href="/author/books/example/viewer/second#section">Duplicate</a>
        '''
        self.assertEqual(converter.extract_chapter_urls(self.book_url, html), [
            f'{self.book_url}/viewer/second',
            f'{self.book_url}/viewer/first',
        ])

    def test_ignores_other_origins(self):
        html = '''
            <a href="https://example.com/author/books/example/viewer/chapter">External</a>
            <a href="http://zenn.dev/author/books/example/viewer/chapter">HTTP</a>
        '''
        self.assertEqual(converter.extract_chapter_urls(self.book_url, html), [])

    def test_ignores_empty_chapter_id(self):
        html = '<a href="/author/books/example/viewer//">Empty</a>'
        self.assertEqual(converter.extract_chapter_urls(self.book_url, html), [])

    def test_ignores_other_books_and_nested_paths(self):
        html = '''
            <a href="/author/books/other/viewer/chapter">Other book</a>
            <a href="/author/books/example/viewer/chapter/extra">Nested</a>
            <a href="/author/books/example">Top</a>
        '''
        self.assertEqual(converter.extract_chapter_urls(self.book_url, html), [])


def content_css(css):
    rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
    return '\n'.join(rule.serialize() for rule in converter.filter_content_rules(rules))


class ContentCssTests(unittest.TestCase):
    def test_keeps_content_rules_and_discards_unrelated_rules(self):
        self.assertEqual(
            content_css('.navigation { color: red; } .znc p { color: blue; }'),
            '.znc p { color: blue; }',
        )

    def test_preserves_nested_media_and_supports_rules(self):
        css = '''
            @media screen {
                .navigation { color: red; }
                .znc p { color: blue; }
                @supports (display: grid) { .znc { display: grid; } }
            }
            @media print { .navigation { display: none; } }
        '''
        filtered = content_css(css)
        self.assertIn('@media screen', filtered)
        self.assertIn('.znc p { color: blue; }', filtered)
        self.assertIn('@supports (display: grid)', filtered)
        self.assertIn('.znc { display: grid; }', filtered)
        self.assertNotIn('.navigation', filtered)
        self.assertNotIn('@media print', filtered)
        rules = tinycss2.parse_stylesheet(filtered, skip_whitespace=True)
        self.assertEqual([rule.type for rule in rules], ['at-rule'])

    def test_builds_epub_with_filtered_css_and_chapter_content(self):
        with tempfile.TemporaryDirectory() as directory:
            book_dir = Path(directory)
            (book_dir / 'book.json').write_text(json.dumps({
                'title': 'Test book', 'author': 'author',
                'chapters': [{'title': 'Chapter', 'file': '001.md'}],
            }), encoding='utf-8')
            (book_dir / '001.md').write_text('# Chapter\n\n本文です。\n', encoding='utf-8')
            css = content_css('@media screen { .znc { color: blue; } }')
            (book_dir / 'style.css').write_text(css, encoding='utf-8')
            output = book_dir / 'book.epub'

            converter.build_epub(book_dir, output)

            with ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read('EPUB/style/style.css').decode(), css)
                chapter = archive.read('EPUB/chapter_001.xhtml').decode()
                self.assertIn('本文です。', chapter)
                self.assertIn('class="znc"', chapter)
                self.assertIn('href="style/style.css"', chapter)


if __name__ == '__main__':
    unittest.main()
