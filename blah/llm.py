import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

def generate_response(prompt: str, context: str = "") -> str:
    system_instruction = (
        "You are an HR & Cambodian Workplace Rights Assistant for Mekong Apparel Co., Ltd. "
        "Answer user questions accurately based on Cambodian Labour Law and company rules. "
        "Always cite article numbers when providing legal information."
    )
    
    full_prompt = f"Context:\n{context}\n\nUser Question:\n{prompt}" if context else prompt
    
    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=full_prompt,
        config={
            "system_instruction": system_instruction,
            "temperature": 0.2,
        }
    )
    return response.text

if __name__ == "__main__":
    # Test the API
    reply = generate_response("What is the standard overtime pay rate on Sundays?")
    print("Gemini Reply:\n", reply)