/*
 * Plan 页面的"草案 -> 确认 -> 保存"流程（第三阶段 C2）。
 *
 * 状态机（避免按钮重复点击造成多次请求 / UI 混乱）：
 *   idle -> generating -> draft_ready -> confirming -> saving -> saved
 * 错误状态（不进入流转，只展示提示）：
 *   confirmation_expired / confirmation_mismatch / active_plan_exists /
 *   validation_error / server_error
 *
 * 安全边界（与 C1 的 confirmation 机制对齐）：
 * - 可信的"用户已确认"状态完全由服务端 confirmation service 产生：
 *     POST /plan/confirmation -> POST /plan/confirmation/confirm -> POST /plan
 * - 前端只在确认流程中持有临时 confirmation_id，不写 URL、不进长期存储
 * - 浏览器只调用业务 API，不接触 Agent 工具协议（Dispatcher / Tool Schema）
 * - 业务规则（validate_plan / difficult_previous_task / active 唯一）全部在服务端
 *
 * sessionStorage 约定（延续项目现有做法）：
 * - current_plan：当前计划（服务端保存成功后以服务端返回为准，detail/study/feedback 继续使用）
 * - plan_draft：待确认的草案临时状态（刷新后可恢复展示；确认时服务端重新签发凭证）
 * - completed_subtask_indices / completed_subtask_plan_key：原有行为，不改动
 */

const PLAN_STATE = { current: "idle" };

function setPlanState(state) {
    PLAN_STATE.current = state;
}

function isPlanState(...states) {
    return states.includes(PLAN_STATE.current);
}

/*
 * 本地日期（YYYY-MM-DD）：浏览器本地时区语义，不使用 UTC，
 * 与页面上的 local-date.js 展示保持一致。
 */
function getLocalDateString() {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
}

/*
 * 统一的请求封装：非 2xx 时抛出 { error_type, message, status }，
 * 由调用方按业务错误类型展示用户可理解的提示。
 */
async function requestJson(url, options = {}) {
    const config = { headers: {}, ...options };

    if (config.body !== undefined) {
        config.headers["Content-Type"] = "application/json";
        config.body = JSON.stringify(config.body);
    }

    let response;

    try {
        response = await fetch(url, config);
    } catch (networkError) {
        throw {
            error_type: "network_error",
            message: "无法连接服务器，请检查网络后重试",
            status: 0
        };
    }

    let data = {};

    try {
        data = await response.json();
    } catch (parseError) {
        data = {};
    }

    if (!response.ok) {
        throw {
            error_type: data.error_type || "server_error",
            message: data.message || "",
            status: response.status
        };
    }

    return data;
}

const PLAN_ERROR_TEXT = {
    confirmation_required: "请先确认学习计划。",
    confirmation_expired: "计划确认已过期，请重新生成计划。",
    confirmation_not_found: "确认凭证无效，请重新点击确认。",
    confirmation_already_used: "该确认已使用，请刷新页面查看今天的计划。",
    confirmation_mismatch: "计划内容发生变化，请重新生成并确认。",
    active_plan_exists: "今天已经存在学习计划。",
    validation_error: "计划没有通过校验",
    history_unavailable: "暂时无法读取学习反馈，请稍后重试。",
    network_error: "无法连接服务器，请检查网络后重试"
};

function planErrorText(error) {
    if (error.error_type === "validation_error" && error.message) {
        return "计划没有通过校验：" + error.message;
    }

    if (PLAN_ERROR_TEXT[error.error_type]) {
        return PLAN_ERROR_TEXT[error.error_type];
    }

    return "当前服务暂时不可用，请稍后重试。";
}

function showPlanSection(sectionId) {
    ["loading", "planContent", "draftContent", "idleContent"].forEach(id => {
        document.getElementById(id).style.display = id === sectionId ? "block" : "none";
    });
}

function showPlanError(text, extraHtml = "") {
    showPlanSection("idleContent");
    document.getElementById("planError").innerHTML = text + extraHtml;
    document.getElementById("planError").style.display = "block";
}

function hidePlanError() {
    document.getElementById("planError").style.display = "none";
}

function setButtonLoading(button, loading, idleText) {
    button.disabled = loading;
    button.textContent = loading ? "处理中..." : idleText;
}

/*
 * 视图：服务端已保存的计划（刷新恢复 / active_plan_exists 后查看）
 */
function showServerPlan(plan) {
    hidePlanError();

    document.getElementById("taskTitle").textContent = plan.task;
    document.getElementById("taskMeta").textContent =
        "数学二 · 预计" + plan.estimated_minutes + "分钟 · 已保存";
    document.getElementById("taskReason").textContent = plan.reason;

    const savedTag = document.getElementById("savedTag");
    if (savedTag) {
        savedTag.style.display = "inline-block";
    }

    showPlanSection("planContent");
    setPlanState("saved");
}

/*
 * 视图：待确认草案（明确告知用户"这是待确认计划，尚未保存"）
 */
function showDraft(draft) {
    hidePlanError();

    const difficultyText = { easy: "简单", medium: "中等", hard: "较难" }[draft.plan.difficulty] || draft.plan.difficulty;

    document.getElementById("draftAnswer").textContent = draft.answer || "";
    document.getElementById("draftTask").textContent = draft.plan.task;
    document.getElementById("draftMeta").textContent =
        "预计 " + draft.plan.estimated_minutes
        + " 分钟 · 难度：" + difficultyText
        + " · " + draft.plan.question_count + " 道题";

    const list = document.getElementById("draftSubtasks");
    list.innerHTML = "";

    (draft.plan.subtasks || []).forEach((subtask, index) => {
        const item = document.createElement("div");
        item.className = "other-task";
        item.innerHTML =
            '<div>'
            + '<div class="other-title">' + (index + 1) + ". " + escapePlanHtml(subtask.title) + "</div>"
            + '<div class="other-meta">' + subtask.minutes + " 分钟"
            + (subtask.question_count > 0 ? " · " + subtask.question_count + " 道题" : "")
            + "</div>"
            + "</div>"
            + '<div class="other-icon">📌</div>';
        list.appendChild(item);
    });

    const criteria = document.getElementById("draftCriteria");
    criteria.textContent = draft.plan.completion_criteria
        ? "完成标准：" + draft.plan.completion_criteria
        : "";

    showPlanSection("draftContent");
    setPlanState("draft_ready");

    const confirmButton = document.getElementById("confirmButton");
    const regenerateButton = document.getElementById("regenerateButton");
    setButtonLoading(confirmButton, false, "确认并保存");
    setButtonLoading(regenerateButton, false, "重新生成");
    confirmButton.disabled = false;
}

/*
 * 页面初始化：服务端优先（GET /plan 恢复），其次恢复待确认草案，否则进入 idle
 */
async function initPlanPage() {
    setPlanState("loading");
    showPlanSection("loading");
    document.getElementById("loading").textContent = "正在加载今日计划...";

    try {
        const result = await requestJson("/plan?date=" + getLocalDateString());

        if (result.exists && result.plan) {
            sessionStorage.setItem(
                "current_plan",
                JSON.stringify({ ...result.plan })
            );
            showServerPlan(result.plan);
            return;
        }
    } catch (error) {
        // 读取失败不阻塞草案流程，进入 idle 后仍可生成
        console.error("读取今日计划失败：", error);
    }

    const draftText = sessionStorage.getItem("plan_draft");

    if (draftText) {
        try {
            showDraft(JSON.parse(draftText));
            return;
        } catch (error) {
            sessionStorage.removeItem("plan_draft");
        }
    }

    showPlanSection("idleContent");
    hidePlanError();
    setPlanState("idle");
}

/*
 * 生成 Plan Draft（只生成，不保存）
 */
async function generatePlanDraft() {
    if (isPlanState("generating", "confirming", "saving")) {
        return;
    }

    setPlanState("generating");
    showPlanSection("loading");
    hidePlanError();
    document.getElementById("loading").textContent = "AI 正在根据你的反馈生成今日计划...";

    const generateButton = document.getElementById("generateButton");
    if (generateButton) {
        setButtonLoading(generateButton, true, "生成今日计划草案");
    }

    try {
        const draft = await requestJson("/plan/draft", { method: "POST" });

        sessionStorage.setItem(
            "plan_draft",
            JSON.stringify({ answer: draft.answer, plan: draft.plan })
        );

        showDraft(draft);
    } catch (error) {
        showPlanError(planErrorText(error));
        setPlanState("idle");
    } finally {
        if (generateButton) {
            setButtonLoading(generateButton, false, "生成今日计划草案");
        }
    }
}

/*
 * 用户点击"确认并保存"：
 *   POST /plan/confirmation -> POST /plan/confirmation/confirm -> POST /plan
 * 三步全部由服务端产生可信确认状态；任何一步失败都按业务错误提示。
 */
async function confirmAndSavePlan() {
    if (!isPlanState("draft_ready")) {
        return;
    }

    setPlanState("confirming");
    hidePlanError();

    const confirmButton = document.getElementById("confirmButton");
    const regenerateButton = document.getElementById("regenerateButton");
    setButtonLoading(confirmButton, true, "确认并保存");
    regenerateButton.disabled = true;

    const draftText = sessionStorage.getItem("plan_draft");
    let draft = null;

    try {
        draft = JSON.parse(draftText || "null");
    } catch (error) {
        draft = null;
    }

    if (!draft || !draft.plan) {
        showPlanError("草案数据已失效，请重新生成计划。");
        setPlanState("idle");
        return;
    }

    const planDate = getLocalDateString();

    try {
        // 1) 创建待确认凭证
        const created = await requestJson("/plan/confirmation", {
            method: "POST",
            body: { plan: draft.plan, plan_date: planDate }
        });

        // 2) 用户确认（服务端把凭证标记为 confirmed）
        await requestJson("/plan/confirmation/confirm", {
            method: "POST",
            body: { confirmation_id: created.confirmation_id }
        });

        // 3) 保存（携带 confirmation_id，服务端校验确认状态与绑定）
        const saved = await requestJson("/plan", {
            method: "POST",
            body: {
                plan: draft.plan,
                plan_date: planDate,
                confirmation_id: created.confirmation_id
            }
        });

        setPlanState("saved");

        // 以服务端保存成功的计划为准，同步 sessionStorage（detail/study/feedback 使用）
        sessionStorage.setItem(
            "current_plan",
            JSON.stringify({
                ...saved.plan,
                plan_id: saved.plan_id,
                plan_date: saved.plan_date
            })
        );
        sessionStorage.removeItem("plan_draft");

        window.location.href = "/app/detail.html";
    } catch (error) {
        setButtonLoading(confirmButton, false, "确认并保存");
        regenerateButton.disabled = false;

        if (error.error_type === "active_plan_exists") {
            showPlanError(
                planErrorText(error),
                ' <a href="#" onclick="viewTodayPlan(); return false;" style="color:#28513b;">查看今天计划</a>'
            );
        } else if (error.error_type === "confirmation_expired") {
            showPlanError(planErrorText(error));
            sessionStorage.removeItem("plan_draft");
            setPlanState("idle");
            return;
        } else {
            showPlanError(planErrorText(error));
        }

        // 回到草案可重试状态（凭证是服务端状态，重新点击会重新签发）
        setPlanState("draft_ready");
    }
}

/*
 * 查看今天已保存的计划（active_plan_exists 提示里提供）
 */
async function viewTodayPlan() {
    try {
        const result = await requestJson("/plan?date=" + getLocalDateString());

        if (result.exists && result.plan) {
            sessionStorage.setItem(
                "current_plan",
                JSON.stringify({ ...result.plan })
            );
            sessionStorage.removeItem("plan_draft");
            showServerPlan(result.plan);
        }
    } catch (error) {
        showPlanError(planErrorText(error));
    }
}

/*
 * 查看任务（已保存计划 -> 任务详情）
 */
function startTask() {
    window.location.href = "/app/detail.html";
}

/*
 * 防止 AI 返回的文本直接插入 HTML
 */
function escapePlanHtml(text) {
    if (text === null || text === undefined) {
        return "";
    }

    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
