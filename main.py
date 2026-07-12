import os
import sys
import json
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import httpx
from datetime import datetime
import asyncio

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

app = FastAPI(title="PVS Exam Management Portal Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CODE_RESULT_CACHE = {}

@app.on_event("startup")
async def startup_event():
    app.state.httpx_client = httpx.AsyncClient(
        verify=True,
        http2=True,
        timeout=httpx.Timeout(10.0, connect=5.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )

@app.on_event("shutdown")
async def shutdown_event():
    client = getattr(app.state, "httpx_client", None)
    if client:
        await client.aclose()

class MarksRequest(BaseModel):
    national_id: str = Field(..., min_length=16, max_length=16)

class CodeSelectionRequest(BaseModel):
    national_id: str
    selected_code: str

class SimpleCodeRequest(BaseModel):
    registration_code: str

IREMBO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "rw,en-US;q=0.9,en;q=0.8",
    "Origin": "https://irembo.gov.rw",
    "Referer": "https://irembo.gov.rw/user/citizen/service/rnp/check_exam_result",
    "Servicecode": "CHECK_EXAM_RESULT",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Ch-Ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"'
}

def format_exam_date(date_str: str) -> str:
    if not date_str:
        return "N/A"

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if fmt == "%Y-%m-%d %H:%M":
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return dt.strftime("%B %d, %Y")
        except ValueError:
            continue

    return date_str


def format_test_center(exam_centers):
    if not exam_centers:
        return "N/A"

    center = exam_centers[0]
    center_name = center.get("name", "")
    location_name = center.get("locationName", "")
    if center_name and location_name:
        return f"{center_name} ({location_name})"
    return center_name or location_name or "N/A"


async def fetch_code_details(client, code):
    if code in CODE_RESULT_CACHE:
        return CODE_RESULT_CACHE[code]

    headers = IREMBO_HEADERS.copy()
    headers["Registrationcode"] = code
    details = {
        "registrationCode": code,
        "status": "N/A",
        "examType": "UNKNOWN",
        "examTypeRaw": "UNKNOWN",
        "isPractical": False,
        "isTheory": False,
        "licenseCategory": "N/A",
        "examDate": "N/A",
        "testCenter": "N/A",
        "marksObtained": 0,
        "totalMarks": 0,
        "passMark": 0,
        "passed": False,
        "grade": "N/A",
        "candidateName": "N/A",
        "nationalId": "N/A"
    }

    try:
        response = await client.get(
            "https://irembo.gov.rw/irembo/rest/public/police/v2/request/exam/registration/registration-code",
            headers=headers,
            timeout=10.0,
        )

        if response.status_code == 200:
            detail_data = response.json()
            if detail_data.get("status") and "data" in detail_data:
                reg = detail_data["data"].get("dlExamRegistration")
                if reg:
                    schedule = reg.get("dlExamSchedule", {})
                    exam = reg.get("dlExamination", {})
                    candidate = reg.get("dlExamCandidate", {})

                    exam_type = schedule.get("examType", "UNKNOWN")
                    details.update({
                        "status": reg.get("status", "N/A"),
                        "examType": "Practical" if exam_type == "PRACTICAL" else "Theory" if exam_type == "THEORY" else exam_type,
                        "examTypeRaw": exam_type,
                        "isPractical": exam_type == "PRACTICAL",
                        "isTheory": exam_type == "THEORY",
                        "licenseCategory": schedule.get("licenseCategoryName", "N/A"),
                        "examDate": format_exam_date(schedule.get("examStartDate") or schedule.get("examEndDate") or ""),
                        "testCenter": format_test_center(schedule.get("examCenters", [])),
                        "marksObtained": exam.get("gainedMark", 0),
                        "totalMarks": exam.get("totalMark", 20),
                        "passMark": exam.get("passMark", 20),
                        "passed": exam.get("grade") == "PASS",
                        "grade": exam.get("grade", "N/A"),
                        "candidateName": f"{candidate.get('firstName', '')} {candidate.get('lastName', '')}".strip() or "N/A",
                        "nationalId": candidate.get("nid", "N/A"),
                    })
    except Exception:
        pass

    CODE_RESULT_CACHE[code] = details
    return details

@app.post("/api/check-marks")
async def check_marks(payload: MarksRequest):
    url = "https://irembo.gov.rw/irembo/rest/public/police/v2/request/applicant-dl-registration-code"
    headers = IREMBO_HEADERS.copy()
    headers["Nationalid"] = payload.national_id
    
    client = app.state.httpx_client
    try:
        response = await client.get(url, headers=headers)

        if response.status_code == 404 or response.status_code == 400:
            return {
                "status": "error",
                "code": "INVALID_ID",
                "message": "❌ INVALID NATIONAL ID\n\nThe provided National ID could not be found in the system. Please verify that you have entered the correct 16-digit ID."
            }

        if response.status_code != 200:
            return {
                "status": "error",
                "code": "SERVER_ERROR",
                "message": f"❌ SERVER ERROR\n\nUnable to connect to the verification system. Status code: {response.status_code}"
            }

        res_data = response.json()

        if not res_data.get("status"):
            return {
                "status": "error",
                "code": "INVALID_ID",
                "message": "❌ INVALID NATIONAL ID\n\nThe provided National ID could not be found in the system. Please verify that you have entered the correct 16-digit ID."
            }

        if "data" not in res_data or "registrationCodes" not in res_data["data"]:
            return {
                "status": "error",
                "code": "NO_CODES",
                "message": "❌ NO EXAM RECORDS\n\nNo driving exam records found for this National ID. This could mean:\n• You haven't registered for any driving exam yet\n• Your registration is still being processed\n• Please check back later or contact the licensing authority"
            }

        codes_list = res_data["data"]["registrationCodes"]
        if not codes_list:
            return {
                "status": "error",
                "code": "NO_CODES",
                "message": "❌ NO EXAM RECORDS\n\nNo driving exam records found for this National ID. This could mean:\n• You haven't registered for any driving exam yet\n• Your registration is still being processed\n• Please check back later or contact the licensing authority"
            }

        tasks = [fetch_code_details(client, code) for code in codes_list]
        code_details = await asyncio.gather(*tasks)

        practical_codes = []
        theory_codes = []
        candidate_name = "N/A"
        national_id = payload.national_id

        for result in code_details:
            if result["isPractical"]:
                practical_codes.append(result["registrationCode"])
            else:
                theory_codes.append(result["registrationCode"])

            if result["candidateName"] != "N/A" and candidate_name == "N/A":
                candidate_name = result["candidateName"]
                national_id = result["nationalId"]

        return {
            "status": "success",
            "candidateName": candidate_name,
            "nationalId": national_id,
            "practical_codes": practical_codes,
            "theory_codes": theory_codes,
            "results": {detail["registrationCode"]: detail for detail in code_details}
        }
    except httpx.TimeoutException:
        return {
            "status": "error",
            "code": "TIMEOUT",
            "message": "⏱️ REQUEST TIMEOUT\n\nThe request took too long to complete. Please check your internet connection and try again."
        }
    except Exception as e:
        return {
            "status": "error",
            "code": "SYSTEM_ERROR",
            "message": f"⚠️ SYSTEM ERROR\n\nAn unexpected error occurred: {str(e)}"
        }

@app.post("/api/select-code")
async def select_code(payload: CodeSelectionRequest):
    result = CODE_RESULT_CACHE.get(payload.selected_code)
    if result:
        return {"status": "success", "result": result}

    return {
        "status": "error",
        "message": "Result not available. Please refresh the exam codes list first."
    }

@app.post("/api/fetch-marks")
async def fetch_marks(payload: SimpleCodeRequest):
    url = "https://irembo.gov.rw/irembo/rest/public/police/v2/request/exam/registration/registration-code"
    headers = IREMBO_HEADERS.copy()
    headers["Registrationcode"] = payload.registration_code
    
    client = app.state.httpx_client
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error calling Irembo API: {e}")
        return {"error": str(e)}

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")