import os
import json
import re
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from enum import Enum
from google import genai
from google.genai import types

# ==========================================
# 1. MODELOS DE DADOS E ENUMS
# ==========================================

class SexoEnum(str, Enum):
    MASCULINO = "masculino"
    FEMININO = "feminino"

class NivelAtividadeEnum(str, Enum):
    SEDENTARIO = "sedentario"
    LEVE = "leve"
    MODERADO = "moderado"
    INTENSO = "intenso"

class ObjetivoEnum(str, Enum):
    PERDA_PESO = "perda_peso"
    MANUTENCAO = "manutencao"
    HIPERTROFIA = "hipertrofia"

class PreferenciaAlimentarEnum(str, Enum):
    ONIVORO = "onivoro"
    VEGETARIANO = "vegetariano"
    VEGANO = "vegano"
    LOW_CARB = "low_carb"

class EstiloCulinarioEnum(str, Enum):
    CASEIRO = "caseiro_brasil"
    PRATICO = "pratico_rapido"
    MEDITERRANEO = "mediterraneo"
    ECONOMICO = "economico"

class PerfilUsuarioInput(BaseModel):
    idade: int = Field(28, ge=15, le=100)
    sexo: SexoEnum = SexoEnum.MASCULINO
    peso_kg: float = Field(78.0, gt=30, lt=300)
    altura_cm: float = Field(178.0, gt=100, lt=250)
    nivel_atividade: NivelAtividadeEnum = NivelAtividadeEnum.MODERADO
    objetivo: ObjetivoEnum = ObjetivoEnum.PERDA_PESO
    ritmo_objetivo: Optional[str] = "moderado"
    preferencia: PreferenciaAlimentarEnum = PreferenciaAlimentarEnum.ONIVORO
    estilo_culinario: EstiloCulinarioEnum = EstiloCulinarioEnum.CASEIRO
    alimentos_favoritos: Optional[str] = ""
    alimentos_evitar: Optional[str] = ""
    intolerancias_saude: Optional[List[str]] = []
    horario_acordar: Optional[str] = "07:00"
    horario_dormir: Optional[str] = "23:00"
    horario_treino: Optional[str] = "nenhum"
    habilidade_culinaria: Optional[str] = "pratico"
    orcamento: Optional[str] = "medio"
    refeicoes_por_dia: int = Field(4, ge=3, le=6)
    dias_plano: int = Field(7, ge=1, le=20)
    gemini_api_key: Optional[str] = None

class Macronutrientes(BaseModel):
    proteinas_g: float
    carboidratos_g: float
    gorduras_g: float
    calorias_totais: float

class RefeicaoIA(BaseModel):
    nome_refeicao: str
    titulo_prato: str
    horario_sugerido: str
    calorias_alvo: float
    proteinas_refeicao_g: float
    carboidratos_refeicao_g: float
    gorduras_refeicao_g: float
    ingredientes: List[str]
    modo_preparo: str
    dica_chef: str

class DiaPlano(BaseModel):
    dia: int
    titulo_dia: str
    refeicoes: List[RefeicaoIA]

class PlanoAlimentarResponse(BaseModel):
    tmb: float
    tdee: float
    meta_calorica: float
    macros: Macronutrientes
    dias_total: int
    dias: List[DiaPlano]

class ConsultaFuncionalInput(BaseModel):
    objetivo_especifico: str = Field(..., min_length=3)
    preferencia: Optional[str] = "onivoro"
    gemini_api_key: Optional[str] = None

class AlimentoRecomendado(BaseModel):
    alimento: str
    porcao_sugerida: str
    por_que_funciona: str
    como_consumir: str

class ReceitaTerapeutica(BaseModel):
    titulo: str
    tempo_preparo: str
    ingredientes: List[str]
    modo_preparo: str
    quando_tomar: str

class ConsultaFuncionalResponse(BaseModel):
    titulo_estrategia: str
    explicacao_fisiologica: str
    alimentos_chave: List[AlimentoRecomendado]
    alimentos_evitar: List[str]
    receita_rapida: ReceitaTerapeutica

class TreinoInput(BaseModel):
    nivel: str = "intermediario"
    foco: str = "hipertrofia"
    equipamento: str = "academia"
    tempo_minutos: int = 45
    gemini_api_key: Optional[str] = None

class Exercicio(BaseModel):
    nome: str
    series: str
    repeticoes: str
    descanso: str
    dica_tecnica: str

class TreinoResponse(BaseModel):
    titulo: str
    foco_principal: str
    aquecimento: List[Exercicio]
    treino_principal: List[Exercicio]
    finalizacao: List[Exercicio]

# ==========================================
# 2. MODELOS ATIVOS E EXECUTOR IA
# ==========================================

MODELOS_ATIVOS = [
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
    "gemini-3.5-flash"
]

def extrair_json_seguro(texto: str):
    texto = texto.strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?", "", texto)
        texto = re.sub(r"\n?```$", "", texto)
    return json.loads(texto.strip())

def obter_chave(api_key_param: Optional[str]):
    key = api_key_param or os.getenv("GEMINI_API_KEY")
    if not key or key.strip() == "":
        raise HTTPException(
            status_code=400,
            detail="Chave API do Gemini ausente. Insira sua chave na aba Configurações."
        )
    return key.strip()

def executar_chamada_ia(client: genai.Client, prompt: str):
    ultimo_erro = None
    for modelo in MODELOS_ATIVOS:
        for tentativa in range(2):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.7
                    )
                )
                if response and response.text:
                    return extrair_json_seguro(response.text)
            except Exception as e:
                erro_str = str(e)
                ultimo_erro = erro_str
                if "404" in erro_str or "NOT_FOUND" in erro_str:
                    break
                time.sleep(1.0)

    raise HTTPException(
        status_code=503,
        detail=f"Erro ao comunicar com a IA. Detalhes: {ultimo_erro}"
    )

# ==========================================
# 3. LÓGICA NUTRICIONAL
# ==========================================

def calcular_metas(p: PerfilUsuarioInput):
    if p.sexo == SexoEnum.MASCULINO:
        tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) + 5
    else:
        tmb = (10 * p.peso_kg) + (6.25 * p.altura_cm) - (5 * p.idade) - 161

    fatores = {
        NivelAtividadeEnum.SEDENTARIO: 1.2,
        NivelAtividadeEnum.LEVE: 1.375,
        NivelAtividadeEnum.MODERADO: 1.55,
        NivelAtividadeEnum.INTENSO: 1.725
    }
    tdee = tmb * fatores.get(p.nivel_atividade, 1.55)

    if p.objetivo == ObjetivoEnum.PERDA_PESO:
        deficit = 0.85 if p.ritmo_objetivo == "conservador" else (0.75 if p.ritmo_objetivo == "agressivo" else 0.80)
        meta_calorica = tdee * deficit
    elif p.objetivo == ObjetivoEnum.HIPERTROFIA:
        superavit = 1.08 if p.ritmo_objetivo == "conservador" else (1.20 if p.ritmo_objetivo == "agressivo" else 1.15)
        meta_calorica = tdee * superavit
    else:
        meta_calorica = tdee

    fator_prot = 2.2 if p.objetivo == ObjetivoEnum.HIPERTROFIA else (1.8 if p.objetivo == ObjetivoEnum.PERDA_PESO else 1.6)
    proteinas_g = p.peso_kg * fator_prot
    cal_prot = proteinas_g * 4
    cal_gord = meta_calorica * 0.25
    gorduras_g = cal_gord / 9
    carboidratos_g = max((meta_calorica - (cal_prot + cal_gord)) / 4, 30.0)

    macros = Macronutrientes(
        proteinas_g=round(proteinas_g, 1),
        carboidratos_g=round(carboidratos_g, 1),
        gorduras_g=round(gorduras_g, 1),
        calorias_totais=round(meta_calorica, 0)
    )
    return round(tmb, 1), round(tdee, 1), round(meta_calorica, 1), macros

# ==========================================
# 4. APP FASTAPI E ROTAS
# ==========================================

app = FastAPI(title="NutriCore Pro Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Erro interno do servidor: {str(exc)}"}
    )

@app.get("/manifest.json")
def serve_manifest():
    return FileResponse("manifest.json")

@app.get("/")
def home():
    return FileResponse("index.html")

@app.post("/api/v1/diet/generate", response_model=PlanoAlimentarResponse)
def criar_plano(perfil: PerfilUsuarioInput):
    tmb, tdee, meta_calorica, macros = calcular_metas(perfil)
    api_key = obter_chave(perfil.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Atue como nutricionista clínico avançado e elabore um plano alimentar completo para exatamente {perfil.dias_plano} dia(s).
    
    ESTRUTURA JSON OBRIGATÓRIA:
    Retorne um objeto JSON contendo o campo "dias", onde cada elemento representa um dia com exatamente {perfil.refeicoes_por_dia} refeições.
    Exemplo:
    {{
      "dias": [
        {{
          "dia": 1,
          "titulo_dia": "Dia 1 - Foco em Energia & Adaptação",
          "refeicoes": [
            {{
              "nome_refeicao": "Café da Manhã",
              "titulo_prato": "Ovos Mexidos com Aveia e Fruta",
              "horario_sugerido": "07:30",
              "calorias_alvo": 420.0,
              "proteinas_refeicao_g": 28.0,
              "carboidratos_refeicao_g": 35.0,
              "gorduras_refeicao_g": 14.0,
              "ingredientes": ["3 ovos", "30g aveia", "1 banana"],
              "modo_preparo": "Bata os ovos e prepare na frigideira. Sirva com banana e aveia.",
              "dica_chef": "Adicione canela para saciedade."
            }}
          ]
        }}
      ]
    }}

    Diretrizes para o período ({perfil.dias_plano} dias):
    - Gere {perfil.dias_plano} dia(s) com variedade inteligente de preparações, respeitando o estilo {perfil.estilo_culinario.value}.
    - Calorias Alvo por Dia: ~{meta_calorica} kcal | Macros: {macros.proteinas_g}g Proteína, {macros.carboidratos_g}g Carbo, {macros.gorduras_g}g Gordura.
    - Preferência: {perfil.preferencia.value}.
    - Alimentos favoritos: {perfil.alimentos_favoritos or 'Nenhum'}.
    - Alimentos a evitar: {perfil.alimentos_evitar or 'Nenhum'}.
    - Condições clínicas: {', '.join(perfil.intolerancias_saude) if perfil.intolerancias_saude else 'Nenhuma'}.

    Retorne APENAS o JSON puro.
    """

    resultado_json = executar_chamada_ia(client, prompt)
    
    # Tratamento caso a IA retorne diretamente uma lista ou um dicionário com "dias"
    if isinstance(resultado_json, list):
        lista_dias_raw = resultado_json
    elif isinstance(resultado_json, dict) and "dias" in resultado_json:
        lista_dias_raw = resultado_json["dias"]
    else:
        # Fallback se retornar refeições soltas
        lista_dias_raw = [{"dia": 1, "titulo_dia": "Dia 1 - Plano Principal", "refeicoes": resultado_json}]

    dias_objs = []
    for item in lista_dias_raw:
        if "refeicoes" in item:
            refeicoes = [RefeicaoIA(**r) for r in item["refeicoes"]]
            dias_objs.append(DiaPlano(dia=item.get("dia", len(dias_objs)+1), titulo_dia=item.get("titulo_dia", f"Dia {len(dias_objs)+1}"), refeicoes=refeicoes))

    return PlanoAlimentarResponse(
        tmb=tmb,
        tdee=tdee,
        meta_calorica=meta_calorica,
        macros=macros,
        dias_total=len(dias_objs),
        dias=dias_objs
    )

@app.post("/api/v1/nutrition/consult", response_model=ConsultaFuncionalResponse)
def consultar_nutricao(dados: ConsultaFuncionalInput):
    api_key = obter_chave(dados.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Atue como nutricionista funcional e especialista em fitoterapia/compostos bioativos.
    Gere um protocolo terapêutico em JSON para o objetivo: "{dados.objetivo_especifico}".
    Padrão alimentar: {dados.preferencia}.

    Estrutura JSON obrigatória:
    {{
      "titulo_estrategia": "Título profissional da estratégia",
      "explicacao_fisiologica": "Explicação científica clara e concisa",
      "alimentos_chave": [
        {{"alimento": "Nome do alimento", "porcao_sugerida": "Quantidade", "por_que_funciona": "Motivo bioquímico", "como_consumir": "Como incluir na rotina"}}
      ],
      "alimentos_evitar": ["Item 1", "Item 2"],
      "receita_rapida": {{
        "titulo": "Nome da receita funcional ou shot",
        "tempo_preparo": "3 min",
        "ingredientes": ["Item 1", "Item 2"],
        "modo_preparo": "Instruções de preparo",
        "quando_tomar": "Horário ideal de consumo"
      }}
    }}
    Retorne APENAS o JSON puro.
    """

    dados_funcionais = executar_chamada_ia(client, prompt)
    return ConsultaFuncionalResponse(**dados_funcionais)

@app.post("/api/v1/workout/generate", response_model=TreinoResponse)
def criar_treino(dados: TreinoInput):
    api_key = obter_chave(dados.gemini_api_key)
    client = genai.Client(api_key=api_key)

    prompt = f"""
    Crie uma sessão de treino completa e personalizada em JSON.
    Nível: {dados.nivel} | Foco: {dados.foco} | Equipamento: {dados.equipamento} | Duração: {dados.tempo_minutos} minutos.

    Estrutura JSON obrigatória:
    {{
      "titulo": "Título da Sessão de Treino",
      "foco_principal": "{dados.foco}",
      "aquecimento": [
        {{"nome": "Exercício de aquecimento", "series": "2", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Orientação de execução"}}
      ],
      "treino_principal": [
        {{"nome": "Exercício principal", "series": "4", "repeticoes": "10-12", "descanso": "60s", "dica_tecnica": "Orientação de postura e ritmo"}}
      ],
      "finalizacao": [
        {{"nome": "Exercício de core ou alongamento", "series": "3", "repeticoes": "45s", "descanso": "45s", "dica_tecnica": "Orientação técnica"}}
      ]
    }}
    Retorne APENAS o JSON puro.
    """

    dados_treino = executar_chamada_ia(client, prompt)
    return TreinoResponse(**dados_treino)
