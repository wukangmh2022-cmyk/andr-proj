from openai import OpenAI

client = OpenAI(
  base_url="https://api.minimaxi.com/v1", 
  api_key="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJHcm91cE5hbWUiOiLml6AiLCJVc2VyTmFtZSI6IuaXoCIsIkFjY291bnQiOiIiLCJTdWJqZWN0SUQiOiIxODc5MTc0MjAwMjM3NzY5NDIwIiwiUGhvbmUiOiIxNTMyODM0OTc2MSIsIkdyb3VwSUQiOiIxODc5MTc0MjAwMjI5MzgwODEyIiwiUGFnZU5hbWUiOiIiLCJNYWlsIjoiIiwiQ3JlYXRlVGltZSI6IjIwMjUtMTEtMDUgMDk6MDY6NDkiLCJUb2tlblR5cGUiOjEsImlzcyI6Im1pbmltYXgifQ.f287iGrA8gktJWoEYBT9LzQIK_R0QFJe9_G4V5crwbrUD6itXvveSzEBU-mEYCE-ecIm1qKHtiyF1iCew6pHPb2avy78yy-hJIQFhs_c9GAS84FKjJyWpbEnGGwsEgHEnGudReyGW9HtVAbns3DVSSCzjO2LSS9KciRxvu6YcF2r2SwKrHh9khuQPq8Fk8Nc2jDj5NuEUmQxBITpXHIzHGSo4n9LhlpzP8Jp2Mu8gxoUizmt-2R2mxDub4d1b-5KiWnR0bZsRjhGhNUqTItoQ2mfXQxq-EcIVQSfdS6t-rwQqRVvNg33T1j-lAQiK5LxAVaTU46MwwFEqCVNIO7-kw"
)

response = client.chat.completions.create(
    model="MiniMax-M2",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hi, how are you?"},
    ],
    # 设置 reasoning_split=True 将思考内容分离到 reasoning_details 字段
    extra_body={"reasoning_split": True},
)

print(f"Thinking:\n{response.choices[0].message.reasoning_details[0]['text']}\n")
print(f"Text:\n{response.choices[0].message.content}\n")