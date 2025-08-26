from fastapi import FastAPI, File, UploadFile
from bs4 import BeautifulSoup
import uvicorn
import re
from collections import Counter
from typing import List

app = FastAPI()

# Regex patterns
FAILURE_LINE_PATTERN = re.compile(
    r"(status code was:\s*(\d+),\s*expected:\s*(\d+),\s*response time in milliseconds:\s*(\d+),\s*url:\s*(\S+),\s*response:)",
    re.IGNORECASE
)
ALT_FAILURE_PATTERN = re.compile(
    r"Expected\s*:\s*(\d+);\s*But it was\s*:(\d+)",
    re.IGNORECASE
)
CUCUMBER_URL_PATTERN = re.compile(r"(https?://[^\s\"']+)", re.IGNORECASE)
FEATURE_LINE_PATTERN = re.compile(r"([A-Z]+-[A-Z]*\d+)\.feature", re.IGNORECASE)
TEST_CASE_PATTERN = re.compile(r"\b([A-Z]+-[A-Z]*\d+)\b", re.IGNORECASE)
ADDITIONAL_PATTERN = re.compile(r"\b([A-Z]{2,}-[A-Z]*\d+)\b", re.IGNORECASE)


def parse_single_file(soup: BeautifulSoup):
    results = []

    # --- STEP 1: Build testcase → URL mapping ---
    cucumber_urls = {}
    for element in soup.find_all("div", class_="element"):
        case_id = None
        case_text = element.get_text(" ", strip=True)
        match = (
            FEATURE_LINE_PATTERN.search(case_text)
            or TEST_CASE_PATTERN.search(case_text)
            or ADDITIONAL_PATTERN.search(case_text)
        )
        if match:
            case_id = match.group(1)

        url = None
        for output_div in element.find_all("div", class_="output"):
            text_block = output_div.get_text(" ", strip=True)
            url_match = CUCUMBER_URL_PATTERN.search(text_block)
            if url_match:
                url = url_match.group(1)
                break

        if case_id and url:
            cucumber_urls[case_id] = url

    # --- STEP 2: Parse failures from all text ---
    all_text = soup.get_text(separator="\n", strip=True)
    lines = all_text.splitlines()

    for i, line in enumerate(lines):
        match = FAILURE_LINE_PATTERN.search(line)
        alt_match = ALT_FAILURE_PATTERN.search(line)

        if match:
            status_code = match.group(2)
            expected_code = match.group(3)
            url = match.group(5)
        elif alt_match:
            expected_code = alt_match.group(1)
            status_code = alt_match.group(2)
            url = "URL Not Found"
        else:
            continue

        # Find nearest testcase ID
        test_case_id = None
        for j in range(max(0, i - 5), min(len(lines), i + 15)):
            feat_match = FEATURE_LINE_PATTERN.search(lines[j])
            if feat_match:
                test_case_id = feat_match.group(1)
                break
            test_match = TEST_CASE_PATTERN.search(lines[j])
            if test_match:
                test_case_id = test_match.group(1)
                break
            additional_match = ADDITIONAL_PATTERN.search(lines[j])
            if additional_match:
                test_case_id = additional_match.group(1)
                break

        failure_type = f"HTTP {status_code} (expected {expected_code})"

        # ✅ Use cucumber URL if exists
        if test_case_id and test_case_id in cucumber_urls:
            url = cucumber_urls[test_case_id]

        # Capture error context
        context_lines = []
        for j in range(max(0, i), min(len(lines), i + 8)):
            if lines[j].strip():
                context_lines.append(lines[j])

        results.append({
            "status_info": failure_type,
            "testcase": test_case_id or "",
            "url": url,
            "error_analysis": "\n".join(context_lines)
        })

    # --- STEP 3: Add failures without status codes ---
    reported_testcases = {f["testcase"] for f in results if f["testcase"]}

    for element in soup.find_all("div", class_="element"):
        case_id = None
        case_text = element.get_text(" ", strip=True)
        match = (
            FEATURE_LINE_PATTERN.search(case_text)
            or TEST_CASE_PATTERN.search(case_text)
            or ADDITIONAL_PATTERN.search(case_text)
        )
        if match:
            case_id = match.group(1)

        if case_id and case_id not in reported_testcases:
            url = cucumber_urls.get(case_id, "URL Not Found")
            context_lines = element.get_text("\n", strip=True).splitlines()

            results.append({
                "status_info": "no status code",
                "testcase": case_id,
                "url": url,
                "error_analysis": "\n".join(context_lines[:12])
            })
            reported_testcases.add(case_id)

    # --- STEP 4: Deduplicate ---
    unique_failures = {}
    failures_without_testcase = []
    for failure in results:
        testcase = failure["testcase"]
        status_info = failure["status_info"]
        if testcase:
            unique_key = f"{testcase}_{status_info}"
            if unique_key not in unique_failures:
                unique_failures[unique_key] = failure
        else:
            failures_without_testcase.append(failure)

    unique_results = list(unique_failures.values())
    status_counts = Counter(f["status_info"] for f in unique_results)

    return {
        "total_unique": len(unique_results),
        "status_summary": dict(status_counts),
        "failures": unique_results
    }


@app.post("/parse_failures")
async def parse_failures(files: List[UploadFile] = File(...)):
    all_results = []

    for idx, file in enumerate(files, start=1):
        contents = await file.read()
        soup = BeautifulSoup(contents, "html.parser")
        parsed = parse_single_file(soup)

        #  Use unique ID since filenames are same
        all_results.append({
            "file_id": f"file_{idx}",
            "filename": file.filename,
            **parsed
        })

    return {
        "files_processed": len(all_results),
        "results": all_results
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
