from openai import OpenAI

with open("api_key.txt", "r", encoding="utf-8") as f:
    key = f.read()

client = OpenAI(
  base_url="https://openrouter.ai/api/v1",
  api_key=key,
)

with open("prompt.md", "r", encoding="utf-8") as f:
    prompt = f.read()

text = "Зачем вы в Данию столько дронов посылаете?  Не буду больше.  Не надо, больше не буду.  Не буду больше ни во Франции, ни в Дании, ни в Кэппингаген.  Куда еще ты летаешь?  Везде.  Да, в леса Бонны.  Куда не летаешь?  Знаете, там развлекаются люди,  которые когда-то развлекались по поводу  неопознанных летающих объектов, НЛО.  Но там столько чудаков.  Как и у нас, кстати говоря,  ничем не отличается, особенно молодые люди.  Там они сейчас будут запускать вам каждый день.  Каждый день, боже, вот и по условиям, все это.  Но, как вы понимаете, если серьезно,  у нас и дронов-то нет, которые дали Собонна долетают.  Есть определенные и большой дальности,  но целей там нет, что самое-то главное.  Вот и, в общем, детка."
content = prompt + "\n\n" + text

completion = client.chat.completions.create(
  model="google/gemma-3-27b-it:free",
  messages=[
    {
      "role": "system",
      "content": [
        {
          "type": "text",
          "text": prompt
        }
      ]
          },
              {
                "role": "user",
                "content": [
                  {
                    "type": "text",
                    "text": text
                  }
                ]
              }
            ]
)
print(completion.choices[0].message.content)