import os
import time
import uuid
import random
import string
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, EmailStr
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Signup QA Studio", version="2.3.0")

# MOCK-ONLY QA: this service never contacts paid2watch.co or api.appzmagic.com.
events = deque(maxlen=500)
seen_emails = set()
request_times = deque()
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
NAMES = ["Akash", "Raju", "Nisha", "Priya", "Rahul", "Ananya", "Rohan", "Maya", "Amit", "Neha", "Arjun", "Puja"]


class SignupPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    country_iso: str = Field(default="IN", min_length=2, max_length=2)
    device_platform: str = Field(default="", max_length=40)
    referral_code: Optional[str] = Field(default=None, max_length=64)
    referrer_id: Optional[int] = None
    source_app: str = Field(default="mock-test-harness", max_length=64)
    cookie_id: Optional[str] = Field(default=None, max_length=128)
    device_fp_hash: Optional[str] = Field(default=None, max_length=256)


class BulkPayload(BaseModel):
    count: int = Field(ge=1, le=50)
    source_app: str = Field(default="mock-bulk-test", max_length=64)
    referral_code: Optional[str] = Field(default=None, max_length=64)
    referrer_id: Optional[int] = None


@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "mode": "mock-tests-plus-single-owner-qa-run",
        "external_signup_enabled": True,
        "browser_automation": "Playwright Chromium headless on Render; CAPTCHA is not bypassed",
    }


@app.get("/api/events")
def get_events():
    return {"events": list(events), "count": len(events)}


def _check_rate_limit():
    now = time.time()
    while request_times and now - request_times[0] > 60:
        request_times.popleft()
    if len(request_times) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Mock test rate limit reached.")
    request_times.append(now)


def _mock_signup(payload: SignupPayload):
    _check_rate_limit()
    email = str(payload.email).lower()
    request_id = str(uuid.uuid4())
    if email in seen_emails:
        result = {
            "ok": False,
            "status": 409,
            "message": "Duplicate email (simulated)",
            "request_id": request_id,
        }
        status = "duplicate"
    else:
        seen_emails.add(email)
        result = {
            "ok": True,
            "status": 201,
            "message": "Mock signup accepted; no real account was created.",
            "request_id": request_id,
            "user": {"email": email, "display_name": payload.display_name},
            "test_metadata": {
                "source_app": payload.source_app,
                "referral_code": payload.referral_code,
                "referrer_id": payload.referrer_id,
            },
        }
        status = "accepted"

    # Do not log passwords, cookies, or fingerprint values.
    events.appendleft({
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "email": email,
        "name": payload.display_name,
        "result": status,
        "source_app": payload.source_app,
        "referral_code": payload.referral_code,
        "referrer_id": payload.referrer_id,
        "request_id": request_id,
    })
    return result


@app.post("/api/test/signup")
def mock_signup(payload: SignupPayload):
    return _mock_signup(payload)


@app.post("/api/test/bulk-signups")
def mock_bulk_signups(payload: BulkPayload):
    """Sequential simulated signups only; reserved example.test emails are used."""
    results = []
    for i in range(payload.count):
        first = random.choice(NAMES)
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        email = f"{first.lower()}.{suffix}@example.test"
        data = SignupPayload(
            email=email,
            password="MockOnly@123",
            display_name=first,
            country_iso="IN",
            device_platform="web",
            referral_code=payload.referral_code,
            referrer_id=payload.referrer_id,
            source_app=payload.source_app,
            cookie_id=f"mock-{uuid.uuid4().hex}",
            device_fp_hash=f"mock-fingerprint-{uuid.uuid4().hex}",
        )
        try:
            res = _mock_signup(data)
            results.append({
                "number": i + 1,
                "ok": res["ok"],
                "email": email,
                "name": first,
                "referral_code": payload.referral_code,
                "referrer_id": payload.referrer_id,
                "message": res["message"],
            })
        except HTTPException as exc:
            results.append({
                "number": i + 1, "ok": False, "email": email,
                "name": first, "message": str(exc.detail),
            })
            break
    success_count = sum(1 for item in results if item["ok"])
    all_ok = success_count == payload.count
    return {
        "ok": all_ok,
        "requested": payload.count,
        "completed": len(results),
        "success_count": success_count,
        "message": "SUCCESS ALL — all mock signups completed." if all_ok
                   else "Batch finished with errors; see results.",
        "results": results,
    }


@app.post("/api/test/reset")
def reset_mock():
    seen_emails.clear()
    events.clear()
    request_times.clear()
    return {"ok": True, "message": "Mock test data cleared."}


class LiveSignupTestPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(min_length=1, max_length=60)


@app.post("/api/browser/signup-test")
async def live_signup_test(payload: LiveSignupTestPayload):
    """Run one owner-authorized browser test against the configured signup flow.

    This does not bypass CAPTCHA. If CAPTCHA blocks automated submission, the
    response reports that outcome for the site owner to inspect. Passwords are
    never written to event logs or returned in the response.
    """
    _check_rate_limit()
    run_id = uuid.uuid4().hex[:10]
    browser = None
    result = {
        "ok": False,
        "run_id": run_id,
        "mode": "single-site-owner-qa-test",
        "referral_url": "https://paid2watch.co/r/78jnd",
        "signup_url": "https://paid2watch.co/#/signup",
        "email": str(payload.email).lower(),
    }
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            console_errors = []
            page.on("pageerror", lambda exc: console_errors.append(str(exc)[:250]))

            await page.goto(result["referral_url"], wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
            referral_storage = await page.evaluate("""() => {
                const entries = Object.entries(localStorage);
                return Object.fromEntries(entries.filter(([k]) =>
                    /refer|referrer|invite|affiliate|source/i.test(k)
                ));
            }""")
            all_storage_keys = await page.evaluate("() => Object.keys(localStorage)")

            await page.goto(result["signup_url"], wait_until="domcontentloaded", timeout=60000)
            await page.locator("#display-name").fill(payload.display_name)
            await page.locator("#email").fill(str(payload.email))
            await page.locator("#password").fill(payload.password)

            captcha_detected = await page.locator(
                'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="challenges.cloudflare.com" i], [data-sitekey], input[name*="captcha" i]'
            ).count() > 0
            before_url = page.url
            await page.get_by_role("button", name="Create free account").click(timeout=15000)
            await page.wait_for_timeout(3500)

            body_text = (await page.locator("body").inner_text())[:2500]
            # Look for visible validation / CAPTCHA indicators without guessing success.
            lower_text = body_text.lower()
            captcha_blocked = captcha_detected and any(term in lower_text for term in [
                "captcha", "verify you are human", "verification required", "complete the challenge"
            ])
            success_indicators = any(term in lower_text for term in [
                "account created", "signup successful", "registration successful", "welcome"
            ])
            error_indicators = any(term in lower_text for term in [
                "invalid", "required", "already exists", "failed", "try again", "error"
            ])
            result.update({
                "ok": bool(success_indicators and not captcha_blocked),
                "final_url": page.url,
                "url_changed": page.url != before_url,
                "referral_storage": referral_storage,
                "local_storage_keys": all_storage_keys,
                "captcha_detected_before_submit": captcha_detected,
                "captcha_blocked_or_requested": captcha_blocked,
                "success_text_detected": success_indicators,
                "validation_or_error_text_detected": error_indicators,
                "page_text_excerpt": body_text,
                "page_errors": console_errors[:10],
                "message": (
                    "Success-like text detected; verify the new test account in your admin/database." if success_indicators and not captcha_blocked
                    else "CAPTCHA appears to be blocking or requesting verification; this headless test does not bypass it." if captcha_blocked or captcha_detected
                    else "Form submitted; inspect page_text_excerpt and page_errors to determine the site response."
                ),
            })
            events.appendleft({
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "email": str(payload.email).lower(),
                "name": payload.display_name,
                "result": "live-test-success-like" if result["ok"] else "live-test-needs-review",
                "source_app": "playwright-owner-qa",
                "referral_code": None,
                "referrer_id": None,
                "request_id": run_id,
            })
    except PlaywrightTimeoutError as exc:
        result["error"] = f"Browser timeout: {str(exc)[:250]}"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:350]}"
    finally:
        if browser:
            await browser.close()
    return result


@app.post("/api/test/browser-run")
async def browser_run():
    run_id = uuid.uuid4().hex[:10]
    test_email = f"playwright-{run_id}@example.test"
    result = {
        "ok": False,
        "engine": "Playwright Chromium",
        "mode": "headless (Render server)",
        "test_email": test_email,
    }
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            port = os.getenv("PORT", "8000")
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=25000)
            await page.locator("#email").fill(test_email)
            await page.locator("#password").fill("MockOnly@123")
            await page.locator("#display").fill("Playwright Test")
            await page.locator("#source").fill("playwright-mock")
            await page.locator("#singleReferral").fill("TESTCODE")
            await page.locator("#singleReferrer").fill("")
            await page.get_by_role("button", name="Run mock signup").click()
            await page.wait_for_function(
                "() => document.querySelector('#response').textContent.includes('Mock signup accepted')",
                timeout=12000,
            )
            result.update({
                "ok": True,
                "message": "Playwright mock browser test passed.",
                "ui_response": await page.locator("#response").inner_text(),
            })
    except PlaywrightTimeoutError:
        result["error"] = "Timed out waiting for the mock form response."
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:350]}"
    finally:
        if browser:
            await browser.close()
    events.appendleft({
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "email": test_email,
        "name": "Playwright Test",
        "result": "browser-run-passed" if result["ok"] else "browser-run-failed",
        "source_app": "playwright-mock",
        "referral_code": None,
        "referrer_id": None,
        "request_id": run_id,
    })
    return result
