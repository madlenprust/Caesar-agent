"""SkillExecutor — применяет скиллы (exact_recipe) без LLM где возможно.

Скилл может содержать шаги 3 типов:
  1. script — выполнить shell команду напрямую (0 токенов LLM)
  2. llm — LLM генерирует что-то (код, текст) — только этот шаг стоит токены
  3. tool — вызвать конкретный инструмент (web_search, memory_add_fact, etc.)
  4. check — проверить что результат совпадает с ожидаемым

Гибридное выполнение:
  - Скриптовые шаги = 0 токенов (детерминированно)
  - LLM шаги = только где нужна генерация (не весь рецепт)
  - anti_patterns = если шаг упал, проверяем не повторяем ли старую ошибку

Сценарий:
  Пользователь: "настрой nginx для сайта example.com"
  → find_skill находит "setup_nginx" v3 (confidence: high)
  → SkillExecutor выполняет recipe:
    step 1 (script): apt install nginx ✓
    step 2 (script): cat > /etc/nginx/sites-available/example.conf << EOF... ✓
    step 3 (check): nginx -t → expected "syntax is ok" ✓
    step 4 (script): systemctl restart nginx ✓
  → 0 вызовов LLM, задача выполнена за 2 секунды

Если шаг упал:
  → проверяем anti_patterns — не эту ли ошибку мы уже видели?
  → если да — пропускаем, пробуем альтернативу
  → если нет — зовём LLM для адаптации (только этот шаг)
"""

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from caesar.logging_setup import get_logger
from caesar.memory.l4 import L4Skills, Skill


@dataclass
class StepResult:
    """Результат выполнения шага recipe."""
    step_index: int
    step_type: str  # script | llm | tool | check
    success: bool
    output: str = ""
    error: str = ""
    tokens_used: int = 0
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class SkillExecutionResult:
    """Результат применения скилла."""
    skill_name: str
    skill_version: int
    success: bool
    steps_total: int
    steps_executed: int
    steps_skipped: int
    steps_failed: int
    tokens_used: int  # суммарно — только LLM шаги
    results: list[StepResult]
    final_output: str
    error: str = ""
    fell_back_to_llm: bool = False  # True если не смогли применить скилл


class SkillExecutor:
    """Выполняет exact_recipe скилла по шагам.
    
    Гибридное выполнение:
    - script шаги → shell_exec (0 токенов)
    - llm шаги → cheap LLM (минимум токенов)
    - tool шаги → прямой вызов инструмента
    - check шаги → сравнение с expected
    
    Если шаг падает:
    1. Проверяем anti_patterns — не эту ли ошибку видели?
    2. Если да — пропускаем (skip), переходим к следующему
    3. Если нет — пытаемся выполнить через LLM (fallback для этого шага)
    4. Если и LLM не помог — весь скилл падает, fallback на обычный flow
    """
    
    def __init__(self, l4_skills: L4Skills, llm_router=None, tool_registry=None):
        self.l4 = l4_skills
        self.llm = llm_router
        self.tools = tool_registry
        self.log = get_logger("skill_executor")
    
    async def try_apply_skill(
        self,
        user_message: str,
        user_id: str,
        channel_id: str,
    ) -> SkillExecutionResult | None:
        """Попытаться найти и применить скилл.
        
        Возвращает:
        - SkillExecutionResult если скилл найден и выполнен (успешно или нет)
        - None если подходящего скилла нет
        """
        # 1. Ищем скилл
        matches = await self.l4.find_skill(user_message, confidence_threshold="high")
        if not matches:
            self.log.info(f"No skill match for: '{user_message[:60]}...'")
            return None
        
        skill_data = matches[0]
        skill = self.l4.get_skill(skill_data["name"])
        if not skill:
            return None
        
        confidence = skill_data.get("confidence", "medium")
        self.log.info(
            f"Skill match: '{skill.name}' v{skill.version} "
            f"(confidence: {confidence}, success: {skill.success_count}, "
            f"fail: {skill.failure_count})"
        )
        
        # Если скилл помечен broken — не применяем
        if skill.broken:
            self.log.warning(f"Skill '{skill.name}' is broken, skipping")
            return None
        
        # 2. Выполняем recipe
        result = await self._execute_recipe(skill, user_message, user_id, channel_id)
        
        # 3. Записываем результат
        if result.success:
            self.l4.record_success(skill.name)
            self.log.info(
                f"Skill '{skill.name}' applied successfully "
                f"({result.steps_executed}/{result.steps_total} steps, "
                f"{result.tokens_used} tokens)"
            )
        else:
            self.l4.record_failure(skill.name, result.error)
            # Добавляем anti_pattern если это новая ошибка
            if result.error and not result.fell_back_to_llm:
                self._check_and_add_anti_pattern(skill, result)
        
        return result
    
    async def _execute_recipe(
        self,
        skill: Skill,
        user_message: str,
        user_id: str,
        channel_id: str,
    ) -> SkillExecutionResult:
        """Выполнить recipe по шагам."""
        results: list[StepResult] = []
        total_tokens = 0
        final_output = ""
        
        # Извлекаем переменные из user_message для подстановки в recipe
        variables = self._extract_variables(user_message, skill)
        self.log.info(f"Extracted variables: {variables}")
        
        for i, step in enumerate(skill.exact_recipe):
            step_type = step.get("type", "script")
            step_desc = step.get("description", step.get("desc", f"step {i+1}"))
            
            self.log.info(f"Skill '{skill.name}' step {i+1}/{len(skill.exact_recipe)}: {step_desc} (type={step_type})")
            
            # Проверяем anti_patterns — не этот ли шаг мы уже сломали?
            if self._matches_anti_pattern(step, skill):
                results.append(StepResult(
                    step_index=i,
                    step_type=step_type,
                    success=False,
                    skipped=True,
                    skip_reason="Matches anti_pattern — known broken step",
                ))
                self.log.warning(f"Step {i+1} skipped — matches anti_pattern")
                continue
            
            # Выполняем шаг
            step_result = await self._execute_step(
                step, step_type, variables, user_message, user_id, channel_id, i,
            )
            results.append(step_result)
            total_tokens += step_result.tokens_used
            
            if not step_result.success and not step_result.skipped:
                # Шаг упал — пробуем LLM fallback для этого шага
                self.log.warning(
                    f"Step {i+1} failed: {step_result.error}. "
                    f"Trying LLM fallback for this step..."
                )
                
                fallback_result = await self._llm_fallback_step(
                    step, step_result.error, variables, user_message, i,
                )
                if fallback_result and fallback_result.success:
                    results[-1] = fallback_result  # заменяем на успешный
                    total_tokens += fallback_result.tokens_used
                    self.log.info(f"Step {i+1} recovered via LLM fallback")
                else:
                    # Не смогли восстановить — скилл падает
                    self.log.error(f"Step {i+1} failed completely, skill aborts")
                    return SkillExecutionResult(
                        skill_name=skill.name,
                        skill_version=skill.version,
                        success=False,
                        steps_total=len(skill.exact_recipe),
                        steps_executed=i,
                        steps_skipped=sum(1 for r in results if r.skipped),
                        steps_failed=1,
                        tokens_used=total_tokens,
                        results=results,
                        final_output="",
                        error=f"Step {i+1} failed: {step_result.error}",
                        fell_back_to_llm=True,
                    )
            
            # Накапливаем output
            if step_result.output:
                final_output += step_result.output + "\n"
        
        return SkillExecutionResult(
            skill_name=skill.name,
            skill_version=skill.version,
            success=True,
            steps_total=len(skill.exact_recipe),
            steps_executed=sum(1 for r in results if not r.skipped),
            steps_skipped=sum(1 for r in results if r.skipped),
            steps_failed=0,
            tokens_used=total_tokens,
            results=results,
            final_output=final_output,
        )
    
    async def _execute_step(
        self,
        step: dict,
        step_type: str,
        variables: dict,
        user_message: str,
        user_id: str,
        channel_id: str,
        step_index: int,
    ) -> StepResult:
        """Выполнить один шаг recipe."""
        
        if step_type == "script":
            return await self._execute_script_step(step, variables, step_index)
        elif step_type == "llm":
            return await self._execute_llm_step(step, variables, step_index)
        elif step_type == "tool":
            return await self._execute_tool_step(step, variables, user_id, channel_id, step_index)
        elif step_type == "check":
            return self._execute_check_step(step, variables, step_index)
        else:
            return StepResult(
                step_index=step_index,
                step_type=step_type,
                success=False,
                error=f"Unknown step type: {step_type}",
            )
    
    async def _execute_script_step(
        self, step: dict, variables: dict, step_index: int,
    ) -> StepResult:
        """Выполнить script шаг — shell команда напрямую.
        
        0 токенов LLM. Детерминированно.
        """
        command_template = step.get("command", "")
        if not command_template:
            return StepResult(
                step_index=step_index, step_type="script",
                success=False, error="No 'command' in script step",
            )
        
        # Подстановка переменных: {name} → value
        command = self._substitute_variables(command_template, variables)
        
        # Выполняем через subprocess
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            
            output = stdout.decode("utf-8", errors="replace")
            error = stderr.decode("utf-8", errors="replace")
            success = proc.returncode == 0
            
            # Проверяем expected_output если задан
            expected = step.get("expected_output")
            if expected and success:
                if expected not in output:
                    # Может быть regex
                    try:
                        if not re.search(expected, output):
                            success = False
                            error = f"Expected output '{expected}' not found in: {output[:200]}"
                    except re.error:
                        success = False
                        error = f"Expected '{expected}' not in output"
            
            return StepResult(
                step_index=step_index, step_type="script",
                success=success, output=output, error=error,
            )
        except asyncio.TimeoutError:
            return StepResult(
                step_index=step_index, step_type="script",
                success=False, error="Script timeout (60s)",
            )
        except Exception as e:
            return StepResult(
                step_index=step_index, step_type="script",
                success=False, error=str(e),
            )
    
    async def _execute_llm_step(
        self, step: dict, variables: dict, step_index: int,
    ) -> StepResult:
        """Выполнить llm шаг — LLM генерирует что-то.
        
        Это единственный тип шага который тратит токены.
        Используем cheap LLM если возможно.
        """
        prompt_template = step.get("prompt", "")
        if not prompt_template:
            return StepResult(
                step_index=step_index, step_type="llm",
                success=False, error="No 'prompt' in llm step",
            )
        
        prompt = self._substitute_variables(prompt_template, variables)
        
        if not self.llm or not self.llm.cheap.api_key:
            return StepResult(
                step_index=step_index, step_type="llm",
                success=False, error="No cheap LLM configured",
            )
        
        from caesar.core.llm import LLMMessage
        
        try:
            resp = await self.llm.cheap_chat(
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=step.get("max_tokens", 1000),
            )
            
            output = resp.content.strip()
            
            # Если есть expected_output — проверяем
            expected = step.get("expected_output")
            if expected and expected not in output:
                return StepResult(
                    step_index=step_index, step_type="llm",
                    success=False, output=output,
                    error=f"Expected '{expected}' not in LLM output",
                    tokens_used=resp.total_tokens,
                )
            
            # Если есть save_to — сохраняем результат в variables
            save_to = step.get("save_to")
            if save_to:
                variables[save_to] = output
            
            return StepResult(
                step_index=step_index, step_type="llm",
                success=True, output=output,
                tokens_used=resp.total_tokens,
            )
        except Exception as e:
            return StepResult(
                step_index=step_index, step_type="llm",
                success=False, error=str(e),
            )
    
    async def _execute_tool_step(
        self, step: dict, variables: dict,
        user_id: str, channel_id: str, step_index: int,
    ) -> StepResult:
        """Выполнить tool шаг — вызвать инструмент напрямую."""
        tool_name = step.get("tool", "")
        if not tool_name or not self.tools:
            return StepResult(
                step_index=step_index, step_type="tool",
                success=False, error="No tool name or tools not configured",
            )
        
        # Подготавливаем аргументы
        args_template = step.get("args", {})
        args = {}
        for k, v in args_template.items():
            if isinstance(v, str):
                args[k] = self._substitute_variables(v, variables)
            else:
                args[k] = v
        
        # Устанавливаем контекст
        self.tools.set_context(channel_id=channel_id, user_id=user_id)
        
        try:
            result = await self.tools.execute(tool_name, **args)
            
            if hasattr(result, 'success'):
                success = result.success
                output = str(result.data) if result.data else ""
                error = result.error or ""
            else:
                success = True
                output = str(result)
                error = ""
            
            # save_to
            save_to = step.get("save_to")
            if save_to and success:
                variables[save_to] = output
            
            return StepResult(
                step_index=step_index, step_type="tool",
                success=success, output=output, error=error,
            )
        except Exception as e:
            return StepResult(
                step_index=step_index, step_type="tool",
                success=False, error=str(e),
            )
    
    def _execute_check_step(
        self, step: dict, variables: dict, step_index: int,
    ) -> StepResult:
        """Проверить условие — не выполняет действий."""
        condition = step.get("condition", "")
        value = self._substitute_variables(condition, variables)
        
        # Простая проверка: если value == "true" или не пустой → success
        success = value.lower().strip() in ("true", "yes", "1", "ok", "да")
        
        return StepResult(
            step_index=step_index, step_type="check",
            success=success, output=value,
            error="" if success else f"Check failed: {condition}",
        )
    
    async def _llm_fallback_step(
        self, step: dict, error: str, variables: dict,
        user_message: str, step_index: int,
    ) -> StepResult | None:
        """LLM fallback для упавшего шага.
        
        Если шаг упал, просим LLM адаптировать команду.
        Использует smart LLM (т.к. нужна адаптация).
        """
        if not self.llm or not self.llm.smart.api_key:
            return None
        
        from caesar.core.llm import LLMMessage
        
        step_desc = step.get("description", "unknown step")
        step_command = step.get("command", step.get("prompt", ""))
        
        prompt = (
            f"Шаг '{step_desc}' из recipe упал с ошибкой:\n"
            f"Команда: {step_command}\n"
            f"Ошибка: {error}\n\n"
            f"Адаптируй команду чтобы она сработала. "
            f"Верни ТОЛЬКО исправленную команду, без объяснений."
        )
        
        try:
            resp = await self.llm.smart_chat(
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.3,
                max_tokens=500,
            )
            
            adapted_command = resp.content.strip()
            if not adapted_command or adapted_command == step_command:
                return None  # LLM не смог адаптировать
            
            # Пробуем выполнить адаптированную команду
            self.log.info(f"LLM adapted command: {adapted_command[:100]}")
            
            proc = await asyncio.create_subprocess_shell(
                adapted_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            
            output = stdout.decode("utf-8", errors="replace")
            success = proc.returncode == 0
            
            if success:
                return StepResult(
                    step_index=step_index,
                    step_type=step.get("type", "script"),
                    success=True, output=output,
                    tokens_used=resp.total_tokens,
                )
            else:
                return None
        except Exception as e:
            self.log.warning(f"LLM fallback failed: {e}")
            return None
    
    def _extract_variables(self, user_message: str, skill: Skill) -> dict:
        """Извлечь переменные из user_message для подстановки в recipe.
        
        Простая V1: возвращает предзаготовленные переменные из skill.variables
        + базовые (user_message, timestamp).
        """
        variables = {
            "user_message": user_message,
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        }
        
        # Если в skill есть variables — добавляем
        for var_def in getattr(skill, "variables", []) or []:
            if isinstance(var_def, dict):
                name = var_def.get("name", "")
                pattern = var_def.get("pattern", "")
                default = var_def.get("default", "")
                if name and pattern:
                    try:
                        m = re.search(pattern, user_message)
                        if m:
                            variables[name] = m.group(1) if m.groups() else m.group(0)
                        elif default:
                            variables[name] = default
                    except re.error:
                        pass
        
        return variables
    
    def _substitute_variables(self, template: str, variables: dict) -> str:
        """Подставить переменные в шаблон: {name} → value."""
        result = template
        for key, value in variables.items():
            result = result.replace(f"{{{key}}}", str(value))
        return result
    
    def _matches_anti_pattern(self, step: dict, skill: Skill) -> bool:
        """Проверить — не этот ли шаг уже ломался раньше (anti_pattern)?"""
        step_desc = step.get("description", step.get("command", ""))
        for ap in skill.anti_patterns:
            if isinstance(ap, dict):
                ap_pattern = ap.get("step", ap.get("error", ""))
                if ap_pattern and ap_pattern in step_desc:
                    return True
            elif isinstance(ap, str) and ap in step_desc:
                return True
        return False
    
    def _check_and_add_anti_pattern(self, skill: Skill, result: SkillExecutionResult) -> None:
        """Добавить anti_pattern если это новая ошибка."""
        # Находим первый упавший шаг
        for r in result.results:
            if not r.success and not r.skipped and r.error:
                # Проверяем что такой error ещё не в anti_patterns
                already_known = False
                for ap in skill.anti_patterns:
                    if isinstance(ap, dict) and ap.get("error", "") == r.error:
                        already_known = True
                        break
                
                if not already_known:
                    self.l4.add_anti_pattern(skill.name, r.error)
                    self.log.info(
                        f"Added anti_pattern to '{skill.name}': {r.error[:80]}"
                    )
                break
