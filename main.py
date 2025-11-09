import os, json, requests
from fastapi import FastAPI, Request
from typing import Optional, Dict, Any
import uvicorn
from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

sevs_tool = {
    "type": "function",
    "function": {
        "name": "sevsEligibilityLookup",
        "description": "Query SEVS eligibility, expiry windows, and model-report status.",
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "enum": ["vehicle_eligibility","expiring_soon","model_report_status"]},
                "make": {"type": "string"}, "model": {"type": "string"}, "variant": {"type": "string"},
                "model_code": {"type": "string"}, "build_date": {"type": "string"},
                "build_year": {"type": "integer"}, "build_month": {"type": "integer","minimum":1,"maximum":12},
                "window_days": {"type": "integer"}, "window_months": {"type": "integer"},
                "limit": {"type": "integer","default":20}, "cursor": {"type": "string"}
            },
            "required": ["query_type"]
        }
    }
}

SYSTEM_PROMPT = (
    "You are an Australian SEVS eligibility assistant. Be concise and precise. "
    "Call the sevsEligibilityLookup tool for eligibility/expiring/model report questions. "
    "Extract make, model, variant, model_code, build_date/year/month, window_days/months. "
    "Explain the verdict and why using the tool JSON; ask for variant/model code if ambiguous."
)

def call_sevs(args: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json","apikey": SUPABASE_KEY,"Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.post(SUPABASE_URL, headers=headers, json=args, timeout=30)
    r.raise_for_status()
    return r.json()

def compose_reply(payload: Dict[str, Any]) -> str:
    if not payload.get("ok"):
        return "I couldn’t retrieve SEVS data right now. Try again in a moment."
    data = payload.get("data", [])
    if not data:
        alts = payload.get("alternates") or []
        if alts:
            alt_str = ", ".join([f'{a.get("variant","?")} {a.get("model_code","")}'.strip() for a in alts][:5])
            return f"No exact match. Closest options: {alt_str}. Share the variant/model code and build year/month."
        return "No matching SEVS entry found. Share the variant or model code and build year/month and I’ll re-check."
    if len(data) > 1:
        lines = ["I found multiple matches:"]
        for row in data[:3]:
            line = f'• {row.get("make","")} {row.get("model","")} {row.get("variant","")} {row.get("model_code","")} — '
            line += "Eligible" if row.get("eligible") else "Not eligible"
            if row.get("expires_on"): line += f"; expires {row['expires_on']}"
            mr = row.get("model_report") or {}
            line += f"; MR {mr.get('status','none') if mr.get('has_report') else 'none'}"
            lines.append(line)
        lines.append("Tell me the specific variant or model code and I’ll confirm.")
        return "\n".join(lines)
    row = data[0]
    eligible = row.get("eligible")
    verdict = "Eligible" if eligible else "Not eligible"
    parts = [f"**{verdict}**"]
    if row.get("eligibility_reason"): parts.append(f"Reason: {row['eligibility_reason']}.")
    bd = row.get("build_date_match") or {}
    if bd.get("from") or bd.get("to"): parts.append(f'Build window: {bd.get("from","?")} → {bd.get("to","?")}.')
    if row.get("expires_on"):
        d = f" ({row['days_to_expiry']} days)" if row.get("days_to_expiry") is not None else ""
        parts.append(f"SEVS entry expires {row['expires_on']}{d}.")
    if row.get("expiring_soon"): parts.append("Flag: expiring soon. Start compliance steps ASAP.")
    mr = row.get("model_report") or {}
    if mr.get("has_report"):
        s = f"Model report: {mr.get('status','unknown')}"
        if mr.get("mr_number"): s += f" — {mr['mr_number']}"
        if mr.get("issuer"): s += f" ({mr['issuer']})"
        parts.append(s + ".")
    else:
        parts.append("No valid model report on record; compliance not currently possible.")
    alts = row.get("alternates") or []
    if alts:
        alt_str = ", ".join([f'{a.get("variant","?")} {a.get("model_code","")}'.strip() for a in alts[:3]])
        parts.append(f"Closest alternatives: {alt_str}.")
    return "\n".join(parts)

def send_whatsapp_text(to_number: str, text: str):
    url = "https://graph.facebook.com/v20.0/me/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    body = {"messaging_product":"whatsapp","to":to_number,"type":"text","text":{"preview_url":False,"body":text}}
    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()

@app.get("/webhook")
async def verify(mode: Optional[str] = None, challenge: Optional[str] = None, token: Optional[str] = None):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge) if (challenge and challenge.isdigit()) else (challenge or "")
    return "forbidden"

@app.post("/webhook")
async def inbound(request: Request):
    payload = await request.json()
    try:
        entry = payload["entry"][0]["changes[0]"]["value"]  # sometimes value is nested oddly
    except Exception:
        entry = payload.get("entry",[{}])[0].get("changes",[{}])[0].get("value",{})
    messages = entry.get("messages", [])
    if not messages: return {"ok": True}
    msg = messages[0]
    from_number = msg["from"]
    text = msg.get("text", {}).get("body") or msg.get("interactive", {}).get("nfm_reply", {}).get("response_json")
    if not text: return {"ok": True}

    chat = client.chat.completions.create(
        model="gpt-5-chat",
        messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":text}],
        tools=[sevs_tool],
        tool_choice="auto"
    )
    message = chat.choices[0].message
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        args = json.loads(tool_calls[0].function.arguments)
        try:
            data = call_sevs(args)
            reply_text = compose_reply(data)
        except Exception as e:
            reply_text = f"I couldn’t reach the SEVS service. {e}"
    else:
        reply_text = message.content or "Tell me the make/model/variant or model code, and build year/month."
    try:
        send_whatsapp_text(from_number, reply_text)
    except Exception as e:
        print("Send error:", e)
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
