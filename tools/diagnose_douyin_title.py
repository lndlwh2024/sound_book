"""Replay one failed upload through title filling only; never publish or archive."""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from src.publisher.douyin.browser import DouyinBrowserManager, DOUYIN_UPLOAD_URL
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher.douyin.probe import DouyinDOMProbe
from src.publisher.douyin.uploader import DouyinUploader, TITLE_SELECTOR, resolve_publish_series


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    task = json.loads(args.report.read_text(encoding='utf-8'))
    failed = task['failure_files']
    if len(failed) != 1:
        raise RuntimeError('Expected exactly one failed file; refuse batch replay')
    item = failed[0]
    video = Path(item['file_path'])
    if not video.is_file() or video.stat().st_size != item['size_bytes']:
        raise RuntimeError('Failed file is missing or has changed')
    configs = json.loads((ROOT / 'data/douyin_accounts.json').read_text(encoding='utf-8'))
    account = DouyinAccountConfig.from_dict(next(x for x in configs if x['account_id'] == item['account_id']))
    output = ROOT / 'output/playwright' / ('title_' + time.strftime('%Y%m%d_%H%M%S'))
    output.mkdir(parents=True)
    result = {'file': video.name, 'account': account.account_name,
              'published': False, 'archived': False, 'network_errors': [], 'page_errors': []}
    probe = DouyinDOMProbe(output)
    uploader = DouyinUploader(account, progress_callback=lambda msg: print(msg, flush=True))
    context = None
    with sync_playwright() as playwright:
        try:
            print('Opening single-file title diagnostic; no publish operation', flush=True)
            context = DouyinBrowserManager.create_persistent_context(
                playwright, uploader.profile_path, headless=False, timeout=10000)
            context.set_default_timeout(2000)
            page = context.pages[0] if context.pages else context.new_page()
            DouyinBrowserManager.inject_stealth(page)
            def response_received(response):
                if response.status in (403, 429) and len(result['network_errors']) < 30:
                    url = urlsplit(response.url)
                    result['network_errors'].append({'status': response.status, 'url': url.scheme + '://' + url.netloc + url.path})
            page.on('response', response_received)
            page.on('pageerror', lambda error: result['page_errors'].append(str(error)[:300]) if len(result['page_errors']) < 30 else None)
            def request_failed(request):
                if len(result['network_errors']) < 30:
                    url = urlsplit(request.url)
                    result['network_errors'].append({'failure': request.failure, 'url': url.scheme + '://' + url.netloc + url.path})
            page.on('requestfailed', request_failed)
            # Diagnostic navigation has a longer budget than the stop-sensitive deployment.
            page.goto(DOUYIN_UPLOAD_URL, wait_until='domcontentloaded', timeout=30000)
            uploader._pause(page, 3000)
            probe.capture_dom_report(page, diagnostic={'stage': 'before_upload'})
            if 'login' in page.url.lower():
                raise RuntimeError('Login expired; no upload attempted')
            page.locator('input[type="file"][accept*="video"], input[type="file"][accept*=".mp4"]').first.set_input_files(str(video))
            uploader._pause(page, 3000)
            started = time.monotonic()
            title = uploader._wait_for_title(page, 45)
            result['ready_seconds'] = round(time.monotonic() - started, 3)
            DouyinDOMProbe(output / 'before_fill').capture_dom_report(page, diagnostic={'stage': 'before_fill'})
            # Check whether the historical 2-second call still fails in today's page state.
            try:
                page.locator(TITLE_SELECTOR).first.fill(resolve_publish_series(video.name), timeout=2000)
                result['old_fill'] = 'SUCCESS'
            except Exception as error:
                result['old_fill'] = str(error)
            uploader._fill_title(title, resolve_publish_series(video.name))
            result['title'] = title.input_value()
            result['status'] = 'TITLE_FILLED_ONLY'
            DouyinDOMProbe(output / 'after_fill').capture_dom_report(page, diagnostic={'stage': 'after_fill'})
            print('Title verified; stopping before cover, collection, or publish', flush=True)
        except Exception as error:
            result['status'] = 'FAILED'
            result['error'] = str(error)
            if context and context.pages:
                try:
                    probe.capture_dom_report(context.pages[0], diagnostic={'stage': 'title_replay_failed', 'reason': str(error)})
                except Exception as capture_error:
                    result['capture_error'] = str(capture_error)
        finally:
            (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False), flush=True)
            print('Artifacts: ' + str(output), flush=True)
            if context:
                context.close()


if __name__ == '__main__':
    main()
