"""Browser/contract checks for the standalone prototype; no deployed API calls."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PRE_RELOAD_COUNT = 0
SCHEMAS = json.loads((ROOT.parent / 'design/schemas.json').read_text())['$defs']


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def profile(page):
    return page.evaluate('adpPrototype.profile()')


def session(page):
    return page.evaluate('adpPrototype.session()')


def section(page, name):
    page.locator('.tab[data-tab="setup"]').click()
    page.locator(f'.step-nav [data-section="{name}"]').click()


def open_product(page):
    page.locator('[data-action="open-product"]').first.click()


def create(page, name='Review example'):
    page.locator('[data-action="new-product"]').click()
    page.locator('[data-edit="name"]').fill(name)
    page.locator('[data-edit="purpose"]').fill('Coordinate small releases.')
    page.locator('[data-edit="users"]').fill('Maintainers, Release managers')
    page.locator('#create-form button[type="submit"]').click()


def controls(page, **settings):
    page.locator('[data-action="preview"]').click()
    for name, value in settings.items():
        node = page.locator(f'#preview-{name}')
        if isinstance(value, bool):
            node.set_checked(value)
        else:
            node.select_option(value)
    page.locator('[data-action="apply-preview"]').click()


def add_source(page, kind):
    section(page, 'basic' if kind in ('repository', 'tracker', 'document') else 'advanced')
    page.locator(f'[data-action="source"][data-kind="{kind}"]').click()
    page.locator('[data-action="select-source"]').click()


def check_contracts(page):
    evidence = page.evaluate('adpPrototype.evidence()')
    count = 0
    for record in evidence['requests'] + evidence['responses']:
        validator = Draft202012Validator({'$ref': f"#/$defs/{record['schema']}", '$defs': SCHEMAS}, format_checker=FormatChecker())
        errors = list(validator.iter_errors(record['value']))
        require(not errors, f"{record['schema']}: {[e.message for e in errors]}")
        count += 1
    return count


def checkpoint_reload(page):
    global PRE_RELOAD_COUNT
    PRE_RELOAD_COUNT += check_contracts(page)
    page.reload()


def screenshot(page, output, name):
    # Capture sticky chrome at its initial position, not at a form's scroll offset.
    page.evaluate("window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
    page.screenshot(path=str(output / name), full_page=True)


def minimal_create(page, output):
    page.locator('[data-action="new-product"]').click()
    page.locator('[data-edit="name"]').fill('A simple idea')
    page.locator('#create-form button[type="submit"]').click()
    require(page.locator('#create-error').inner_text() == 'Add a short purpose.', 'Name-only Product was accepted')
    page.locator('[data-edit="purpose"]').fill('Help maintainers plan releases.')
    page.locator('#create-form button[type="submit"]').click()
    require('intended user' in page.locator('#create-error').inner_text(), 'Missing audience was accepted')
    page.locator('[data-edit="users"]').fill('Maintainers')
    page.locator('.optional summary').click()
    page.locator('[data-edit="domain"]').fill('Developer tooling')
    page.locator('#create-form button[type="submit"]').click()
    p = profile(page)
    require(p['product']['domain'] == 'Developer tooling', 'Domain was lost on creation')
    require(p['progress']['completed'] == p['progress']['applicable'] == 2, 'Optional onboarding fields became required')
    require(p['progress']['percentage'] == 100 and p['progress']['maturity'] == 'saved', 'Minimal Idea progress is wrong')
    require(p['product']['lifecycle_status'] == 'draft', 'Saving falsely activated the lifecycle')
    require(page.evaluate('scrollY === 0'), 'Creation did not reveal the saved Product heading')
    saved = session(page)
    require(saved['state'] == 'completed', 'Minimal saved session stayed open')
    require(saved['steps']['information']['status'] == 'complete' and saved['steps']['information']['evidence_refs'], 'Saved information lacks completion evidence')
    require(all(saved['steps'][name]['status'] == 'not_started' for name in ('basic', 'advanced', 'financials')), 'Untouched optional sections gained work')
    require(page.locator('#project-selector').count() == 0, 'Single-scope Product exposed an unnecessary selector')
    screenshot(page, output, 'minimal-saved.png')
    page.set_viewport_size({'width':390,'height':1000})
    require(page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'Minimal Product overflowed on mobile')
    screenshot(page, output, 'minimal-saved-mobile.png')
    page.locator('.scope-options summary').click()
    page.locator('[data-action="manage-projects"]').click()
    require(page.locator('[data-action="unbind-project"]').is_disabled(), 'Last scope could be removed')
    page.locator('#new-project-name').fill('Mobile delivery')
    page.locator('[data-action="add-project"]').click()
    require(page.locator('#project-selector').is_visible(), 'Additional scope did not expose the selector')


def project_isolation(page, output):
    open_product(page)
    original = profile(page)['project_id']
    add_source(page, 'repository')
    binding = page.evaluate('adpPrototype.bindings()')['items'][0]
    require(binding['verified_ref']['record_id'] == binding['selection']['native_id'], 'Source reference replaced its native identity')
    require(profile(page)['progress']['maturity'] == 'getting_started', 'Verified repository did not advance maturity')
    section(page, 'financials')
    page.locator('[data-edit="amount"]').fill('75.00')
    page.locator('#financials-form button[type="submit"]').click()
    first = profile(page)
    page.locator('#project-selector').select_option(label='Developer CLI')
    second = profile(page)
    require(second['project_id'] != original, 'Project did not switch')
    require(not second['configuration'].get('basic', {}).get('repositories'), 'Repositories leaked between Projects')
    require(not second['configuration'].get('financials'), 'Financials leaked between Projects')
    require(second['progress']['maturity'] == 'saved', 'Progress leaked between Projects')
    page.locator('#project-selector').select_option(original)
    require(profile(page)['configuration']['financials'] == first['configuration']['financials'], 'Switching erased financials')
    screenshot(page, output, 'financial-plan.png')


def creation_limits_and_shared_edits(page, output):
    page.locator('[data-action="new-product"]').click()
    page.locator('[data-edit="name"]').fill('Boundary example')
    page.locator('[data-edit="purpose"]').fill('Coordinate releases.')
    page.locator('[data-edit="users"]').fill('Maintainers')
    page.locator('.optional summary').click()
    for field, value, expected in (
        ('name', 'n' * 201, '200 characters'),
        ('purpose', 'p' * 10001, '10,000 characters'),
        ('domain', 'd' * 201, '200 characters'),
        ('users', 'u' * 201, '200 characters'),
        ('users', ','.join(f'User {i}' for i in range(101)), '100 intended users'),
    ):
        node = page.locator(f'[data-edit="{field}"]')
        original = node.input_value()
        node.evaluate("el => el.removeAttribute('maxlength')")
        node.fill(value)
        page.locator('#create-form button[type="submit"]').click()
        require(expected in page.locator('#create-error').inner_text(), f'Invalid {field} crossed the save boundary')
        page.locator('.optional summary').click()
        page.locator(f'[data-edit="{field}"]').fill(original)
    page.locator('[data-edit="users"]').fill(', '.join(f'User {i}' for i in range(100)))
    page.locator('#create-form button[type="submit"]').click()
    require(len(profile(page)['product']['intended_users']) == 100, 'Valid audience boundary rejected')
    page.locator('[data-action="products"]').first.click()
    open_product(page)
    section(page, 'information')
    first_scope = profile(page)['project_id']
    page.locator('#project-selector').select_option(label='Developer CLI')
    page.locator('[data-edit="name"]').fill('Shared Product update')
    page.locator('#information-form button[type="submit"]').click()
    page.locator('#project-selector').select_option(first_scope)
    require(page.locator('[data-edit="name"]').input_value() == 'Shared Product update', 'Clean form retained old shared Product data')
    page.locator('[data-action="cli"]').click()
    page.locator('#modal summary').click()
    page.locator('#cli-name').fill('Shared CLI update')
    page.locator('[data-action="cli-edit"]').click()
    require(page.locator('[data-edit="name"]').input_value() == 'Shared CLI update', 'Clean form failed to refresh after CLI edit')


def evidence_expiry_and_recovery(page, output):
    open_product(page)
    add_source(page, 'repository')
    original = page.evaluate('adpPrototype.bindings()')['items'][0]
    original_revision = profile(page)['revision']
    page.clock.set_fixed_time(datetime.now(timezone.utc) + timedelta(days=3))
    section(page, 'basic')
    require('Needs verification' in page.locator('main').inner_text(), 'Expired evidence still looked verified')
    expired = page.evaluate('adpPrototype.bindings()')['items'][0]
    require(expired['status'] == 'degraded' and expired['observed_at'] == original['observed_at'], 'Expiry fabricated a fresh observation')
    require(profile(page)['revision'] == original_revision, 'Freshness read mutated registry revision')
    require(next(c for c in profile(page)['progress']['checks'] if c['id'] == 'repository')['status'] == 'stale', 'Expired evidence was not reported stale')
    require(session(page)['steps']['basic']['status'] == 'blocked' and not session(page)['steps']['basic']['evidence_refs'], 'Expired evidence still certified the section')
    page.locator('.tab[data-tab="context"]').click()
    require('0 verified' in page.locator('main').inner_text(), 'Context counted expired evidence as verified')
    page.locator('.tab[data-tab="capabilities"]').click()
    require('Inputs ready' not in page.locator('main').inner_text(), 'Expired evidence retained service readiness')
    section(page, 'basic')
    page.locator('[data-action="source"][data-kind="repository"]').click()
    require('Evidence needs refreshing' in page.locator('#modal').inner_text(), 'Source review disagreed with expiry status')
    screenshot(page, output, 'expired-evidence.png')
    page.locator('[data-action="close"]').click()
    controls(page, pause=True)
    page.locator('[data-action="source"][data-kind="repository"]').click()
    page.locator('[data-action="verify-source"]').click()
    pending = session(page)['steps']['basic']
    require(pending['status'] == 'waiting_external', 'Reverification did not show a waiting section')
    controls(page, discovery='denied')
    page.locator('[data-action="resume-operation"]').first.click()
    require(session(page)['steps']['basic']['status'] == 'blocked', 'Failed verification did not block the section')
    controls(page, discovery='complete', pause=False)
    page.locator('[data-action="source"][data-kind="repository"]').click()
    page.locator('[data-action="verify-source"]').click()
    require(session(page)['steps']['basic']['status'] == 'complete' and session(page)['state'] == 'completed', 'Successful retry left the old failure blocking progress')
    refreshed = page.evaluate('adpPrototype.bindings()')['items'][0]
    require(refreshed['status'] == 'healthy' and refreshed['observed_at'] != original['observed_at'], 'Real verification did not refresh evidence')
    require(refreshed['verified_ref'] == original['verified_ref'], 'Reverification changed native source identity/revision without a source change')


def binding_maturity_and_resume(page, output):
    open_product(page)
    controls(page, pause=True)
    add_source(page, 'repository')
    p = profile(page)
    require(p['progress']['percentage'] == 66 and p['progress']['maturity'] == 'saved', 'Unverified repository counted as complete')
    pending = session(page)
    require(pending['steps']['basic']['status'] == 'waiting_external' and pending['steps']['basic'].get('operation_id'), 'Pending verification did not reach section state')
    checkpoint_reload(page)
    require(session(page)['id'] == pending['id'], 'Session identity lost on reload')
    require(session(page)['state'] == 'waiting_external', 'Pending handoff lost on reload')
    page.locator('[data-action="resume-operation"]').first.click()
    require(profile(page)['progress']['maturity'] == 'getting_started', 'Resumed verification did not update evidence')
    require(session(page)['steps']['basic']['status'] == 'complete' and session(page)['steps']['basic']['evidence_refs'], 'Verified source lacks section evidence')
    controls(page, pause=False)
    add_source(page, 'environment')
    require(profile(page)['progress']['maturity'] == 'getting_started', 'Environment without pipeline became Advanced')
    add_source(page, 'pipeline')
    require(profile(page)['progress']['maturity'] == 'advanced', 'Environment/pipeline did not give Advanced')
    add_source(page, 'support')
    require(profile(page)['progress']['maturity'] == 'pro', 'Operations context did not give Pro')
    section(page, 'financials')
    page.locator('[data-action="financial-later"]').click()
    require(profile(page)['progress']['percentage'] < 100, 'Later removed a selected check')
    require(session(page)['steps']['financials']['status'] == 'later' and session(page)['state'] == 'open', 'Deferred section was not retained')
    page.locator('[data-action="financial-na"]').click()
    page.locator('[data-action="save-na"]').click()
    require(profile(page)['progress']['percentage'] == 100, 'Valid N/A was not excluded')
    require(session(page)['steps']['financials']['status'] == 'not_applicable' and session(page)['state'] == 'completed', 'N/A section disagrees with completed session')
    screenshot(page, output, 'verified-progress.png')


def import_and_provenance(page, output):
    open_product(page)
    section(page, 'financials')
    page.locator('[data-edit="amount"]').fill('75.00')
    page.locator('#financials-form button[type="submit"]').click()
    original = profile(page)['configuration']['financials']['allocations']
    page.locator('[data-action="review-import"]').click()
    for checkbox in page.locator('.proposal-path').all():
        checkbox.uncheck()
    page.locator('.proposal-path[value="/configuration/financials/release_target_date"]').check()
    page.locator('[data-action="accept-proposal"]').click()
    p = profile(page)
    require(p['configuration']['financials']['allocations'] == original, 'Unselected manual allocation was overwritten')
    require(p['configuration']['financials']['release_target_date'] == '2027-01-18', 'Selected date not accepted')
    remaining = session(page)['proposals']
    require(len(remaining) == 1 and len(remaining[0]['remaining_paths']) == 2, 'Unselected proposal fields were discarded')
    checkpoint_reload(page)
    require(session(page)['proposals'] == remaining, 'Pending suggestions lost on resume')
    methods = {path: item['method'] for item in profile(page)['provenance'] for path in item['paths']}
    require(methods['/configuration/financials/allocations'] == 'manual', 'Manual allocation provenance changed')
    require(methods['/configuration/financials/release_target_date'] == 'extracted', 'Accepted source provenance missing')
    page.locator('[data-action="review-import"]').click()
    screenshot(page, output, 'remaining-suggestions.png')
    page.locator('[data-action="close"]').click()


def conflict_offline_and_uncertain(page, output):
    open_product(page)
    section(page, 'information')
    page.locator('[data-edit="purpose"]').fill('Retain this browser edit.')
    page.locator('[data-action="cli"]').click()
    page.locator('#modal summary').click()
    page.locator('#cli-name').fill('Renamed from CLI')
    page.locator('[data-action="cli-edit"]').click()
    page.locator('#information-form button[type="submit"]').click()
    require(page.locator('#modal-title').inner_text() == 'This profile changed elsewhere', 'Stale form silently overwrote current state')
    screenshot(page, output, 'conflict.png')
    page.locator('[data-action="merge-draft"]').click()
    p = profile(page)
    require(p['product']['name'] == 'Renamed from CLI' and p['product']['purpose'] == 'Retain this browser edit.', 'Explicit merge lost an unrelated edit')
    controls(page, offline=True)
    page.locator('[data-edit="name"]').fill('Offline draft')
    page.locator('#information-form button[type="submit"]').click()
    require(profile(page)['product']['name'] == 'Renamed from CLI', 'Offline input falsely reached saved state')
    checkpoint_reload(page)
    require(page.locator('[data-edit="name"]').input_value() == 'Offline draft', 'Offline draft lost on reload')
    controls(page, offline=False)
    page.locator('#information-form button[type="submit"]').click()
    require(profile(page)['product']['name'] == 'Offline draft', 'Offline retry did not save')
    controls(page, uncertain=True)
    before = profile(page)['revision']
    page.locator('[data-edit="name"]').fill('One accepted save')
    page.locator('#information-form button[type="submit"]').click()
    require(page.locator('[data-edit="name"]').is_disabled(), 'Uncertain save allowed an untracked new edit')
    controls(page, offline=True)
    page.locator('[data-action="check-save"]').click()
    require(page.locator('[data-edit="name"]').is_disabled(), 'Offline confirmation unlocked an uncertain save')
    controls(page, offline=False)
    page.locator('[data-action="check-save"]').click()
    require(profile(page)['revision'] == before + 1, 'Lost response caused duplicate mutation')


def admin_and_revocation(page, output):
    open_product(page)
    controls(page, role='editor', pause=True)
    section(page, 'basic')
    page.locator('[data-action="team"]').click()
    page.locator('#team-choice').select_option('example-platform-team')
    page.locator('[data-action="save-team"]').click()
    require('team_id' not in profile(page)['product'], 'Editor changed team without admin handoff')
    page.locator('[data-action="resume-operation"]').first.click()
    require('team_id' not in profile(page)['product'], 'Polling elevated editor rights')
    controls(page, role='admin', pause=False)
    page.locator('[data-action="resume-operation"]').first.click()
    require(profile(page)['product']['team_id'] == 'example-platform-team', 'Authorized admin could not complete handoff')
    page.locator('[data-action="member"]').click()
    page.locator('[data-action="save-member"]').click()
    page.locator('#project-selector').select_option(label='Developer CLI')
    require('Sam Taylor' in page.locator('main').inner_text(), 'Product team membership did not follow the Project switch')
    checkpoint_reload(page)
    require('Sam Taylor' in page.locator('main').inner_text(), 'Shared team membership was lost on reload')
    add_source(page, 'repository')
    page.locator('[data-action="preview"]').click()
    page.locator('[data-action="revoke-repository"]').click()
    p = profile(page)
    require(not p['configuration'].get('basic', {}).get('repositories'), 'Revoked repository remained in export')
    binding = page.evaluate('adpPrototype.bindings()')['items'][0]
    require(binding['status'] == 'restricted' and 'selection' not in binding, 'Restricted binding leaked source identity')
    require(p['progress']['maturity'] == 'saved', 'Revoked evidence retained maturity')
    require('example-org/planner' not in page.locator('main').inner_text(), 'Revoked source label remained visible')


def project_management_and_layout(page, output):
    open_product(page)
    page.locator('[data-action="manage-projects"]').click()
    page.locator('#new-project-name').fill('Mobile app')
    page.locator('[data-action="add-project"]').click()
    require(profile(page)['project']['name'] == 'Mobile app', 'Add Project did not select new scope')
    page.locator('[data-action="manage-projects"]').click()
    page.locator('summary', has_text='Remove the current').click()
    page.locator('[data-action="unbind-project"]').click()
    require(profile(page)['project']['name'] != 'Mobile app', 'Unbound scope remained selected')
    for width in (320,390,768,1024,1440):
        page.set_viewport_size({'width':width,'height':1000})
        for tab in ('overview','setup','context','capabilities'):
            page.locator(f'.tab[data-tab="{tab}"]').click()
            require(page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), f'Overflow at {width}px on {tab}')
    page.set_viewport_size({'width':390,'height':1000})
    page.locator('[data-action="mobile-nav"]').click()
    require(page.locator('#modal').is_visible(), 'Mobile menu unavailable')
    screenshot(page, output, 'mobile-menu.png')
    page.locator('[data-action="close"]').click()


def main():
    global PRE_RELOAD_COUNT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', help='Existing Chromium executable; otherwise use Playwright Chromium')
    parser.add_argument('--output', type=Path, default=Path('/tmp/adp-onboarding-browser-check'))
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    cases=[minimal_create,project_isolation,creation_limits_and_shared_edits,evidence_expiry_and_recovery,binding_maturity_and_resume,import_and_provenance,conflict_offline_and_uncertain,admin_and_revocation,project_management_and_layout]
    results=[];total=0
    with sync_playwright() as p:
        options={'headless':True}
        if args.browser: options['executable_path']=args.browser
        browser=p.chromium.launch(**options)
        try:
            for test in cases:
                PRE_RELOAD_COUNT = 0
                context=browser.new_context(viewport={'width':1440,'height':1000})
                page=context.new_page();page.set_default_timeout(5000);errors=[];page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto((ROOT/'product-onboarding.html').as_uri())
                try:
                    test(page,args.output)
                    require(not errors,f'Browser errors: {errors}')
                    count=check_contracts(page)+PRE_RELOAD_COUNT;total+=count
                    results.append({'scenario':test.__name__,'result':'pass','contract_examples':count})
                    print(f'PASS: {test.__name__} ({count} contract-shaped requests/responses)',flush=True)
                finally:
                    context.close()
        finally:
            browser.close()
    (args.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(f'PASS: {len(cases)} scenario groups; {total} strict-schema request/response checks. Prototype only; no runtime/provider/installed-CLI verification.')

if __name__=='__main__':
    main()
