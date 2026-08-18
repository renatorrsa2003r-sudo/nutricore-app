import os
import json
import re
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

class PlanoAlimentarResponse(BaseModel):
    tmb: float
    tdee: float
    meta_calorica: float
    macros: Macronutrientes
    refeicoes: List[RefeicaoIA]

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
# 2. UTILITÁRIOS E SEGURANÇA
# ==========================================

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
            detail="Chave API do Gemini não configurada! Vá na aba Configurações e insira sua chave."
        )
    return key.strip()

# ==========================================
# 3. LÓGICA DE CÁLCULO E IA
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

def gerar_com_gemini(perfil: PerfilUsuarioInput, meta_calorica: float, macros: Macronutrientes, api_key: str) -> List[RefeicaoIA]:
    client = genai.Client(api_key=api_key)
    prompt = f"""
    Atue como nutricionista clínico avançado e crie um plano alimentar diário completo e estruturado.
    Formato OBRIGATÓRIO: Retorne estritamente um array JSON com {perfil.refeicoes_por_dia} refeições.
    Exemplo do objeto:
    [
      {{
        "nome_refeicao": "Café da Manhã",
        "titulo_prato": "Ovos Mexidos com Aveia e Fruta",
        "horario_sugerido": "07:30",
        "calorias_alvo": 420.0,
        "proteinas_refeicao_g": 28.0,
        "carboidratos_refeicao_g": 35.0,
        "gorduras_refeicao_g": 14.0,
        "ingredientes": ["3 ovos inteiros", "30g de farelo de aveia", "1 banana prata", "1 colher de café de azeite"],
        "modo_preparo": "Bata os ovos, aqueça a frigideira com o azeite e mexa até o ponto desejado. Consuma com a banana e aveia.",
        "dica_chef": "Adicione canela na banana para melhorar a sensibilidade à insulina."
      }}
    ]

    Dados do Paciente:
    - Peso: {perfil.peso_kg}kg | Altura: {perfil.altura_cm}cm | Idade: {perfil.idade} anos | Sexo: {perfil.sexo.value}
    - Meta Calórica Total: {meta_calorica} kcal
    - Metas de Macronutrientes: Proteínas {macros.proteinas_g}g | Carboidratos {macros.carboidratos_g}g | Gorduras {macros.gorduras_g}g
    - Quantidade exata de refeições: {perfil.refeicoes_por_dia}
    - Estilo Culinário: {perfil.estilo_culinario.value} | Preferência: {perfil.preferencia.value}
    - Horários: Acorda às {perfil.horario_acordar}, Dorme às {perfil.horario_dormir}, Treino: {perfil.horario_treino}
    - Alimentos favoritos: {perfil.alimentos_favoritos or 'Nenhum específico'}
    - Alimentos a evitar / Aversões: {perfil.alimentos_evitar or 'Nenhum'}
    - Foco Clínico / Intolerâncias: {', '.join(perfil.intolerancias_saude) if perfil.intolerancias_saude else 'Nenhuma'}

    Retorne APENAS o JSON puro.
    """
    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    dados = extrair_json_seguro(response.text)
    return [RefeicaoIA(**ref) for ref in dados]

def gerar_consulta_funcional(dados_input: ConsultaFuncionalInput, api_key: str) -> ConsultaFuncionalResponse:
    client = genai.Client(api_key=api_key)
    prompt = f"""
    Atue como nutricionista funcional e especialista em fitoterapia/bioquímica.
    Crie um protocolo terapêutico em JSON para o objetivo: "{dados_input.objetivo_especifico}".
    Preferencia: {dados_input.preferencia}.

    Estrutura JSON:
    {{
      "titulo_estrategia": "Título profissional do protocolo",
      "explicacao_fisiologica": "Explicação detalhada e acessível sobre a via bioquímica",
      "alimentos_chave": [
        {{"alimento": "Cúrcuma com Pimenta Preta", "porcao_sugerida": "1 colher de chá + pitada", "por_que_funciona": "A piperina aumenta a absorção da curcumina em 2000%", "como_consumir": "Em shot matinal ou no almoço"}}
      ],
      "alimentos_evitar": ["Açúcar refinado", "Gorduras hidrogenadas"],
      "receita_rapida": {{
        "titulo": "Shot Anti-inflamatório Matinal",
        "tempo_preparo": "3 min",
        "ingredientes": ["50ml de água morna", "1/2 limão espremido", "1 colher de café de cúrcuma"],
        "modo_preparo": "Misture tudo em um copo pequeno e beba imediatamente em jejum.",
        "quando_tomar": "Ao acordar, 15 min antes do café da manhã"
      }}
    }}
    Retorne APENAS o JSON.
    """
    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    dados = extrair_json_seguro(response.text)
    return ConsultaFuncionalResponse(**dados)

def gerar_treino_com_gemini(dados: TreinoInput, api_key: str) -> TreinoResponse:
    client = genai.Client(api_key=api_key)
    prompt = f"""
    Crie uma rotina de treinamento personalizada estritamente em JSON.
    Nível: {dados.nivel} | Foco: {dados.foco} | Equipamento disponível: {dados.equipamento} | Duração: {dados.tempo_minutos} minutos.

    Estrutura JSON obrigatória:
    {{
      "titulo": "Título da Sessão de Treino",
      "foco_principal": "{dados.foco}",
      "aquecimento": [
        {{"nome": "Mobilidade articular e polichinelos", "series": "2", "repeticoes": "45s", "descanso": "30s", "dica_tecnica": "Aqueça bem ombros e quadris"}}
      ],
      "treino_principal": [
        {{"nome": "Agachamento Livre / Goblet Squat", "series": "4", "repeticoes": "10-12", "descanso": "60s", "dica_tecnica": "Mantenha a coluna neutra e joelhos alinhados com as pontas dos pés"}}
      ],
      "finalizacao": [
        {{"nome": "Prancha Abdominal Isométrica", "series": "3", "repeticoes": "45s", "descanso": "45s", "dica_tecnica": "Contraia glúteos e abdômen firmemente"}}
      ]
    }}
    Retorne APENAS o JSON.
    """
    response = client.models.generate_content(
        model='gemini-3.7-flash',
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    dados = extrair_json_seguro(response.text)
    return TreinoResponse(**dados)

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
    refeicoes = gerar_com_gemini(perfil, meta_calorica, macros, api_key)
    return PlanoAlimentarResponse(
        tmb=tmb,
        tdee=tdee,
        meta_calorica=meta_calorica,
        macros=macros,
        refeicoes=refeicoes
    )

@app.post("/api/v1/nutrition/consult", response_model=ConsultaFuncionalResponse)
def consultar_nutricao(dados: ConsultaFuncionalInput):
    api_key = obter_chave(dados.gemini_api_key)
    return gerar_consulta_funcional(dados, api_key)

@app.post("/api/v1/workout/generate", response_model=TreinoResponse)
def criar_treino(dados: TreinoInput):
    api_key = obter_chave(dados.gemini_api_key)
    return gerar_treino_com_gemini(dados, api_key)
