from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from tg_jobs_monitor.settings import AppConfig, EnvSettings, load_resume_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisResult:
    accepted: bool
    is_vacancy: bool
    role_match: bool
    domain_match: bool
    level_match: bool
    reason: str
    matched_role: Optional[str] = None
    matched_domain: Optional[str] = None
    matched_level: Optional[str] = None
    resume_summary: Optional[str] = None
    resume_fit: Optional[str] = None
    vacancy_title: Optional[str] = None
    company_name: Optional[str] = None
    domain_label: Optional[str] = None
    match_percentage: Optional[int] = None
    responsibilities_summary: tuple[str, ...] = ()
    mismatches: tuple[str, ...] = ()


class VacancyAnalyzer:
    def __init__(self, env: EnvSettings, config: AppConfig) -> None:
        self.env = env
        self.config = config
        self.client = AsyncOpenAI(api_key=env.openai_api_key) if env.openai_api_key else None
        self.resume_text = load_resume_text(env)

    async def analyze(self, text: str) -> AnalysisResult:
        if self.client and self.config.llm.enabled:
            try:
                result = await self._analyze_with_llm(text)
                result = self._apply_product_domain_rule(result, text)
                result = self._apply_pm_publication_rule(result, text)
                result = self._apply_keyword_fit_penalties(result, text)
                return self._apply_location_preference(result, text)
            except Exception:
                logger.exception("LLM analysis failed, falling back to keyword heuristic")
        result = self._analyze_with_keywords(text)
        result = self._apply_product_domain_rule(result, text)
        result = self._apply_pm_publication_rule(result, text)
        result = self._apply_keyword_fit_penalties(result, text)
        return self._apply_location_preference(result, text)

    async def _analyze_with_llm(self, text: str) -> AnalysisResult:
        criteria = self.config.criteria
        trimmed_text = text[: self.config.llm.max_input_chars]
        prompt = {
            "task": "Analyze a Telegram post and decide whether it is a matching job vacancy.",
            "rules": [
                "Do not invent missing information.",
                "Accept if the post is a real job vacancy and clearly matches one of the PM-like role criteria.",
                "Domain criteria are required only when require_domains is true.",
                "Level criteria are required only when require_levels is true.",
                "Do not reject solely because the domain or level is missing when the corresponding requirement is false.",
                "If the role is project manager, delivery manager, program manager, technical project manager, launch manager, implementation manager, or a clear Russian equivalent, accept it even when the domain is not crypto or fintech.",
                "If the title is product manager or product owner, accept only when the description clearly includes project or delivery ownership such as team coordination, scope management, delivery, execution, release planning, stakeholder alignment, deadlines, or cross-functional project leadership.",
                "If the role is product manager or product owner, the domain must explicitly be crypto or web3. Non-crypto and non-web3 product vacancies should be rejected.",
                "Use match_percentage to reflect how well the vacancy fits the resume; do not use rejection instead of a lower score.",
                "For a real PM-like vacancy, keep accepted=true even when the fit is weak because of location, domain mismatch, hard-skill gaps, or language gaps. In such cases lower match_percentage and explain mismatches instead of rejecting.",
                "Reject only clearly unrelated roles, non-vacancies, product-only roles without PM or delivery ownership, sales-only roles, recruiter-only roles, and pure developer roles.",
                "If resume_text is provided, compare the vacancy to the resume.",
                "Do not mention Pavel or any candidate name in the output.",
                "Write concise Russian text for a Telegram digest that speaks directly to the reader when useful.",
                "If company name is not explicit, return null instead of guessing.",
                "Use high match scores conservatively.",
                "90-100 is only for very strong fit where the vacancy is clearly crypto or web3 and the rest of the requirements also mostly match.",
                "If the vacancy is not crypto/web3 focused, the score should usually stay below 90 even when the role is otherwise strong.",
                "If the role is project management but the domain is clearly non-IT and non-digital, such as construction, civil engineering, facilities, manufacturing, industrial, oil and gas, or similar offline sectors, the score should usually stay below 50 unless there is unusually strong overlap with the resume.",
                "Construction project manager and similar offline infrastructure roles are usually a weak fit for this resume and should score below 50.",
                "Vacancies containing 1C, 1С, junior, джуниор, intern, стажер, or стажировка should score low unless the rest of the post explicitly proves a very unusual match.",
                "If the vacancy is centered on DWH, BI, analytics platforms, data engineering, backend infrastructure, or database-heavy delivery rather than product/project coordination in the candidate's domain, the score should usually stay below 50 unless the resume clearly shows matching experience.",
                "When the vacancy explicitly requires hands-on tools or stack that are absent from the resume, such as PostgreSQL, Python, Apache Airflow, MS SQL, DWH, Spark, Kafka, or similar data/backend stack, treat this as a major mismatch and lower the score aggressively.",
                "If there is both a domain mismatch and several hard-skill mismatches, the score should normally stay below 50.",
                "Scores above 50 should mean the vacancy is at least realistically worth considering for application.",
                "If the post is only a short listing or roundup without meaningful vacancy description, responsibilities, requirements, or context, the score should stay below 50 because there is not enough information to justify a strong fit.",
                "If the vacancy is tied to onsite work or relocation abroad and there is no explicit remote option, lower the score aggressively unless the resume clearly supports that geography and language context.",
                "If the vacancy is in Germany, DACH, or another local-language market and the post does not indicate English-only work while the resume does not show the local language, treat this as a major mismatch and keep the score below 50.",
                "The candidate is interested either in remote work or in office work in Saint Petersburg. If the vacancy is explicitly tied to office, hybrid, onsite, or relocation in another city or country and there is no clear remote option, lower the score aggressively.",
                "If the vacancy location is clearly not Saint Petersburg and not remote, treat location as a meaningful mismatch and usually keep the score below 50.",
                "Do not give strong scores to vacancies with missing description; uncertainty should lower the score, not raise it.",
                "If there is too little information to judge fit confidently, use about 50 as the neutral ceiling for an otherwise plausible PM vacancy.",
                "If description is missing and there are no clear positive or negative signals, keep the score around 45-50 rather than above 50.",
                "If description is missing and there are visible mismatch signals such as wrong domain, relocation, onsite format, or missing required language context, keep the score below 50.",
                "Lower the score for meaningful gaps such as stronger English, missing domain depth, or missing required operational experience.",
                "Return only valid JSON.",
            ],
            "criteria": {
                "roles": criteria.roles,
                "domains": criteria.domains,
                "levels": criteria.levels,
                "require_domains": criteria.require_domains,
                "require_levels": criteria.require_levels,
            },
            "required_json_schema": {
                "accepted": "boolean",
                "is_vacancy": "boolean",
                "role_match": "boolean",
                "domain_match": "boolean",
                "level_match": "boolean",
                "reason": "short Russian explanation",
                "matched_role": "string or null",
                "matched_domain": "string or null",
                "matched_level": "string or null",
                "resume_summary": "short Russian summary, 1-3 sentences, or null",
                "resume_fit": "one of strong_fit, partial_fit, weak_fit, unknown, or null",
                "vacancy_title": "short vacancy title in Russian or original language, or null",
                "company_name": "company name if explicitly present, otherwise null",
                "domain_label": "best short domain label such as Fintech, Crypto, Web3, Payments, Blockchain, or null",
                "match_percentage": "integer from 0 to 100 estimating fit to resume, or null",
                "responsibilities_summary": "array of 2-6 short Russian bullet items about what the role involves",
                "mismatches": "array of 0-5 short Russian bullet items about what may be missing for application; if no clear gaps, return empty array",
            },
            "resume_text": self.resume_text,
            "post_text": trimmed_text,
        }
        response = await self.client.chat.completions.create(
            model=self.env.openai_model,
            temperature=self.config.llm.temperature,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise vacancy classification assistant.",
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False),
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        return AnalysisResult(
            accepted=bool(data.get("accepted")),
            is_vacancy=bool(data.get("is_vacancy")),
            role_match=bool(data.get("role_match")),
            domain_match=bool(data.get("domain_match")),
            level_match=bool(data.get("level_match")),
            reason=str(data.get("reason") or "LLM did not provide a reason"),
            matched_role=data.get("matched_role"),
            matched_domain=data.get("matched_domain"),
            matched_level=data.get("matched_level"),
            resume_summary=data.get("resume_summary"),
            resume_fit=data.get("resume_fit"),
            vacancy_title=data.get("vacancy_title"),
            company_name=data.get("company_name"),
            domain_label=data.get("domain_label"),
            match_percentage=_coerce_match_percentage(data.get("match_percentage")),
            responsibilities_summary=_coerce_str_tuple(data.get("responsibilities_summary")),
            mismatches=_coerce_str_tuple(data.get("mismatches")),
        )

    def _analyze_with_keywords(self, text: str) -> AnalysisResult:
        normalized = normalize(text)
        vacancy_markers = [
            "вакансия",
            "ищем",
            "ищу",
            "hiring",
            "job",
            "позиция",
            "открыта роль",
            "open role",
        ]
        is_vacancy = any(marker in normalized for marker in vacancy_markers)
        role = find_pm_like_role(normalized, self.config.criteria.roles)
        domain = find_match(normalized, self.config.criteria.domains)
        level = find_match(normalized, self.config.criteria.levels)
        domain_ok = bool(domain) or not self.config.criteria.require_domains
        level_ok = bool(level) or not self.config.criteria.require_levels
        accepted = bool(is_vacancy and role and domain_ok and level_ok)

        missing = []
        if not is_vacancy:
            missing.append("не похоже на вакансию")
        if not role:
            missing.append("нет подходящей роли")
        if self.config.criteria.require_domains and not domain:
            missing.append("нет подходящей сферы")
        if self.config.criteria.require_levels and not level:
            missing.append("нет middle/senior уровня")

        reason = (
            f"Принято эвристикой: роль={role}, сфера={domain}, уровень={level}."
            if accepted
            else "Отклонено эвристикой: " + "; ".join(missing) + "."
        )
        return AnalysisResult(
            accepted=accepted,
            is_vacancy=is_vacancy,
            role_match=role is not None,
            domain_match=domain is not None,
            level_match=level is not None,
            reason=reason,
            matched_role=role,
            matched_domain=domain,
            matched_level=level,
            resume_summary=None,
            resume_fit=None,
            vacancy_title=extract_title(text),
            company_name=None,
            domain_label=domain.title() if domain else None,
            match_percentage=None,
            responsibilities_summary=(),
            mismatches=(),
        )

    def _apply_product_domain_rule(self, result: AnalysisResult, text: str) -> AnalysisResult:
        normalized = normalize(text)
        if not is_product_role_text(normalized):
            return result
        if has_crypto_web3_domain(normalized):
            return result

        reason = result.reason
        product_reason = " Product-вакансия отклонена: для product обязателен домен crypto или web3."
        if "обязателен домен crypto или web3" not in reason:
            reason += product_reason

        mismatches = list(result.mismatches)
        mismatch = "Для product-вакансии нужен явный домен crypto или web3."
        if mismatch not in mismatches:
            mismatches.append(mismatch)

        capped_score = min(result.match_percentage, 35) if result.match_percentage is not None else 35
        return AnalysisResult(
            accepted=False,
            is_vacancy=result.is_vacancy,
            role_match=result.role_match,
            domain_match=False,
            level_match=result.level_match,
            reason=reason,
            matched_role=result.matched_role,
            matched_domain=result.matched_domain,
            matched_level=result.matched_level,
            resume_summary=result.resume_summary,
            resume_fit=result.resume_fit,
            vacancy_title=result.vacancy_title,
            company_name=result.company_name,
            domain_label=result.domain_label,
            match_percentage=capped_score,
            responsibilities_summary=result.responsibilities_summary,
            mismatches=tuple(mismatches),
        )

    def _apply_pm_publication_rule(self, result: AnalysisResult, text: str) -> AnalysisResult:
        if result.accepted:
            return result
        if not result.is_vacancy or not result.role_match:
            return result
        if is_product_role_text(normalize(text)) and not has_crypto_web3_domain(normalize(text)):
            return result

        reason = result.reason
        pm_override_reason = (
            " PM-вакансия оставлена к публикации; процент fit отражает качество совпадения."
        )
        if "оставлена к публикации" not in reason:
            reason += pm_override_reason

        return AnalysisResult(
            accepted=True,
            is_vacancy=result.is_vacancy,
            role_match=result.role_match,
            domain_match=result.domain_match,
            level_match=result.level_match,
            reason=reason,
            matched_role=result.matched_role,
            matched_domain=result.matched_domain,
            matched_level=result.matched_level,
            resume_summary=result.resume_summary,
            resume_fit=result.resume_fit,
            vacancy_title=result.vacancy_title,
            company_name=result.company_name,
            domain_label=result.domain_label,
            match_percentage=result.match_percentage,
            responsibilities_summary=result.responsibilities_summary,
            mismatches=result.mismatches,
        )

    def _apply_keyword_fit_penalties(self, result: AnalysisResult, text: str) -> AnalysisResult:
        if result.match_percentage is None:
            return result

        normalized = normalize(text)
        penalty_caps: list[tuple[list[str], int, str]] = [
            (
                ["строитель", "construction", "civil engineering", "facilities", "oil and gas"],
                25,
                "Строительная или офлайн-инфраструктурная сфера сильно снижает fit.",
            ),
            (
                ["1с", "1c", "1с:erp", "1c:erp", "1с erp", "1c erp"],
                30,
                "1С-направление сильно снижает fit.",
            ),
            (
                ["junior", "джуниор", "intern", "стажер", "стажировка"],
                20,
                "Junior / стажерский уровень сильно снижает fit.",
            ),
        ]

        capped_score = result.match_percentage
        mismatches = list(result.mismatches)
        for keywords, cap, mismatch in penalty_caps:
            if any(keyword in normalized for keyword in keywords):
                capped_score = min(capped_score, cap)
                if mismatch not in mismatches:
                    mismatches.append(mismatch)

        if capped_score == result.match_percentage and tuple(mismatches) == result.mismatches:
            return result

        return AnalysisResult(
            accepted=result.accepted,
            is_vacancy=result.is_vacancy,
            role_match=result.role_match,
            domain_match=result.domain_match,
            level_match=result.level_match,
            reason=result.reason,
            matched_role=result.matched_role,
            matched_domain=result.matched_domain,
            matched_level=result.matched_level,
            resume_summary=result.resume_summary,
            resume_fit=result.resume_fit,
            vacancy_title=result.vacancy_title,
            company_name=result.company_name,
            domain_label=result.domain_label,
            match_percentage=capped_score,
            responsibilities_summary=result.responsibilities_summary,
            mismatches=tuple(mismatches),
        )

    def _apply_location_preference(self, result: AnalysisResult, text: str) -> AnalysisResult:
        if result.match_percentage is None:
            return result

        normalized = normalize(text)
        if is_preferred_location(normalized):
            return result

        if not has_nonpreferred_location_signal(normalized):
            return result

        capped_score = min(result.match_percentage, 45)
        location_mismatch = (
            "Локация не совпадает: приоритет удаленка или офис в Санкт-Петербурге."
        )
        mismatches = list(result.mismatches)
        if location_mismatch not in mismatches:
            mismatches.append(location_mismatch)

        reason = result.reason
        if "локац" not in reason.casefold():
            reason += " Локация снижает fit: приоритет удаленка или офис в Санкт-Петербурге."

        return AnalysisResult(
            accepted=result.accepted,
            is_vacancy=result.is_vacancy,
            role_match=result.role_match,
            domain_match=result.domain_match,
            level_match=result.level_match,
            reason=reason,
            matched_role=result.matched_role,
            matched_domain=result.matched_domain,
            matched_level=result.matched_level,
            resume_summary=result.resume_summary,
            resume_fit=result.resume_fit,
            vacancy_title=result.vacancy_title,
            company_name=result.company_name,
            domain_label=result.domain_label,
            match_percentage=capped_score,
            responsibilities_summary=result.responsibilities_summary,
            mismatches=tuple(mismatches),
        )


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def find_match(normalized_text: str, terms: list[str]) -> Optional[str]:
    for term in terms:
        normalized_term = normalize(term)
        if len(normalized_term) <= 3:
            pattern = rf"(?<!\w){re.escape(normalized_term)}(?!\w)"
            if re.search(pattern, normalized_text):
                return term
            continue
        if normalized_term in normalized_text:
            return term
    return None


def find_pm_like_role(normalized_text: str, roles: list[str]) -> Optional[str]:
    direct_role = find_match(normalized_text, roles)
    if direct_role is not None:
        return direct_role

    product_terms = [
        "product manager",
        "product owner",
        "продакт менеджер",
        "менеджер продукта",
        "product lead",
    ]
    if not any(normalize(term) in normalized_text for term in product_terms):
        return None

    delivery_signals = [
        "delivery",
        "scope",
        "roadmap execution",
        "release",
        "timeline",
        "deadline",
        "stakeholder",
        "cross-functional",
        "team coordination",
        "управление команд",
        "координац",
        "срок",
        "релиз",
        "delivery manager",
        "project delivery",
        "execution",
        "управление проект",
        "ведение проекта",
        "декомпозици",
        "планирован",
    ]
    if any(normalize(signal) in normalized_text for signal in delivery_signals):
        return "product manager (with PM/delivery scope)"

    return None


def is_product_role_text(normalized_text: str) -> bool:
    product_terms = [
        "product manager",
        "product owner",
        "продакт менеджер",
        "менеджер продукта",
        "product lead",
    ]
    return any(normalize(term) in normalized_text for term in product_terms)


def has_crypto_web3_domain(normalized_text: str) -> bool:
    domain_terms = [
        "crypto",
        "web3",
        "крипт",
        "блокчейн",
        "blockchain",
    ]
    return any(term in normalized_text for term in domain_terms)


def is_preferred_location(normalized_text: str) -> bool:
    remote_keywords = [
        "удален",
        "дистанцион",
        "remote",
        "work from home",
        "home office",
        "anywhere",
        "worldwide",
    ]
    spb_keywords = [
        "санкт-петербург",
        "санкт петербург",
        "спб",
        "saint petersburg",
        "st. petersburg",
        "st petersburg",
        "saint-petersburg",
        "petersburg",
        "питер",
    ]
    return any(keyword in normalized_text for keyword in remote_keywords + spb_keywords)


def has_nonpreferred_location_signal(normalized_text: str) -> bool:
    location_signals = [
        "relocation",
        "релокац",
        "onsite",
        "on-site",
        "office",
        "офис",
        "гибрид",
        "hybrid",
        "локация",
        "location",
        "based in",
        "must be located",
        "only from",
        "только из",
    ]
    return any(signal in normalized_text for signal in location_signals)


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = []
    for item in value:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                items.append(cleaned)
    return tuple(items)


def _coerce_match_percentage(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    return None


def extract_title(text: str) -> Optional[str]:
    for line in text.splitlines():
        cleaned = line.strip(" -•\t")
        if cleaned:
            return cleaned[:140]
    return None
