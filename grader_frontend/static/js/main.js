// Frontend logic for Grader Agent Dashboard

let eventSource = null;
let activeSessionId = null;
let activeUrl = null;

document.addEventListener("DOMContentLoaded", () => {
    init();
});

function init() {
    fetchSubmissions();

    // Event Listeners
    document.getElementById("submission-form").addEventListener("submit", handleFormSubmit);
    document.getElementById("refresh-btn").addEventListener("click", fetchSubmissions);
    document.getElementById("hitl-yes-btn").addEventListener("click", () => handleHitlResponse("yes"));
    document.getElementById("hitl-no-btn").addEventListener("click", () => handleHitlResponse("no"));
    document.getElementById("close-terminal-btn").addEventListener("click", closeTerminal);

    // Check for existing session cookie on load
    const sessionId = getCookie("grader_session_id");
    if (sessionId) {
        logger("Found existing session cookie: " + sessionId);
        // We will check if this session is already completed when we fetch submissions.
        // If it is not in the completed list, we will try to reconnect.
    }
}

// Logger helper for terminal
function logToTerminal(message, type = "info") {
    const terminal = document.getElementById("terminal-body");
    const div = document.createElement("div");
    
    if (type === "error") {
        div.className = "text-red-400 font-medium";
        div.innerHTML = `[ERROR] ${message}`;
    } else if (type === "success") {
        div.className = "text-emerald-400 font-bold text-glow-green";
        div.innerHTML = `[SUCCESS] ${message}`;
    } else if (type === "warning") {
        div.className = "text-yellow-400 font-medium";
        div.innerHTML = `[WARNING] ${message}`;
    } else if (type === "agent") {
        div.className = "text-purple-300";
        div.innerHTML = message;
    } else {
        div.className = "text-gray-300";
        div.innerHTML = message;
    }
    
    terminal.appendChild(div);
    terminal.scrollTop = terminal.scrollHeight;
}

function showTerminal(title = "agent_evaluation.log") {
    document.getElementById("stats-panel").classList.add("hidden");
    document.getElementById("active-run-panel").classList.remove("hidden");
    document.getElementById("terminal-title").textContent = title;
    document.getElementById("status-text").textContent = "running";
    document.getElementById("status-text").className = "text-purple-400 font-mono animate-pulse";
    document.getElementById("status-spinner").className = "fa-solid fa-circle-notch fa-spin text-xs text-purple-400";
    document.getElementById("close-terminal-btn").classList.add("hidden");
}



function formatAgentEvent(eventData) {
    const author = eventData.author || "System";
    let text = "";

    if (eventData.content && eventData.content.parts) {
        text = eventData.content.parts.map(p => p.text || "").join("");
    } else if (typeof eventData.content === "string") {
        text = eventData.content;
    }

    if (!text && eventData.output) {
        if (typeof eventData.output === "string") {
            text = eventData.output;
        } else {
            text = JSON.stringify(eventData.output);
        }
    }

    if (text) {
        // Clean up some common log noise if necessary
        return `<span class="text-purple-400 font-medium">[${author}]</span> ${text}`;
    }
    return null;
}

// Connect to SSE Stream
function connectStream(sessionId, url, isResume = false, responseText = null, interruptId = null) {
    if (eventSource) {
        eventSource.close();
    }

    activeSessionId = sessionId;
    activeUrl = url;
    showTerminal(parseRepoName(url) + " - evaluation.log");

    let streamUrl = `/api/stream/${sessionId}?url=${encodeURIComponent(url)}`;
    if (isResume) {
        streamUrl += `&resume=true&response=${encodeURIComponent(responseText)}&interrupt_id=${encodeURIComponent(interruptId)}`;
    }
    
    logger("Connecting to stream: " + streamUrl);
    eventSource = new EventSource(streamUrl);

    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            const formatted = formatAgentEvent(data);
            if (formatted) {
                logToTerminal(formatted, "agent");
            }
        } catch (e) {
            logToTerminal(event.data);
        }
    };

    eventSource.addEventListener("checkpoint", (event) => {
        logger("Received HITL checkpoint");
        try {
            const data = JSON.parse(event.data);
            showHitlModal(data.message, data.interrupt_id);
            
            // The backend stream will close because the workflow paused.
            // We close our side too. We will reconnect after the user responds.
            eventSource.close();
            document.getElementById("status-text").textContent = "waiting for input";
            document.getElementById("status-text").className = "text-yellow-400 font-mono";
            document.getElementById("status-spinner").className = "fa-solid fa-pause text-xs text-yellow-400";
            logToTerminal("Evaluation paused. Waiting for user confirmation...", "warning");
        } catch (e) {
            logger("Failed to parse checkpoint data: " + e);
        }
    });

    eventSource.addEventListener("complete", (event) => {
        logger("Evaluation complete");
        try {
            const data = JSON.parse(event.data);
            logToTerminal("Evaluation completed successfully!", "success");
            if (data.output && data.output.total_score !== undefined) {
                logToTerminal(`Final Score: ${data.output.total_score}/95`, "success");
            }
        } catch (e) {
            logToTerminal("Evaluation completed.", "success");
        }
        
        eventSource.close();
        activeSessionId = null;
        activeUrl = null;
        
        document.getElementById("status-text").textContent = "completed";
        document.getElementById("status-text").className = "text-emerald-400 font-mono";
        document.getElementById("status-spinner").className = "fa-solid fa-circle-check text-xs text-emerald-400";
        document.getElementById("close-terminal-btn").classList.remove("hidden");
        
        // Refresh the grid to show the new result
        fetchSubmissions();
    });

    eventSource.onerror = (event) => {
        // If we are waiting for HITL, the connection closing is expected, so don't show error.
        if (document.getElementById("status-text").textContent === "waiting for input") {
            return;
        }
        
        logger("SSE Error occurred");
        logToTerminal("Connection to evaluation stream lost or failed.", "error");
        eventSource.close();
        
        document.getElementById("status-text").textContent = "failed";
        document.getElementById("status-text").className = "text-red-400 font-mono";
        document.getElementById("status-spinner").className = "fa-solid fa-circle-xmark text-xs text-red-400";
        document.getElementById("close-terminal-btn").classList.remove("hidden");
        
        activeSessionId = null;
        activeUrl = null;
    };
}

// Handle Form Submit
async function handleFormSubmit(e) {
    e.preventDefault();
    const urlInput = document.getElementById("repo-url");
    const url = urlInput.value.trim();
    if (!url) return;

    await startEvaluation(url);
}

async function startEvaluation(url) {
    const submitBtn = document.getElementById("submit-btn");
    submitBtn.disabled = true;
    submitBtn.innerHTML = `<i class="fa-solid fa-circle-notch fa-spin mr-2"></i> Starting...`;

    try {
        const response = await fetch("/api/submit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: url }),
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || "Failed to submit");
        }

        const data = await response.json();
        logger("Submission successful, session: " + data.session_id);
        
        // Clear terminal
        document.getElementById("terminal-body").innerHTML = "";
        logToTerminal(`Starting evaluation for: ${url}...`);
        
        // Connect to stream
        connectStream(data.session_id, url);
        
        // Clear input
        document.getElementById("repo-url").value = "";
    } catch (error) {
        logger("Submission error: " + error);
        alert("Error starting evaluation: " + error.message);
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = `<span>Start Evaluation</span><i class="fa-solid fa-arrow-right text-xs ml-2"></i>`;
    }
}

// HITL Modal Handling
let currentInterruptId = null;

function showHitlModal(message, interruptId) {
    currentInterruptId = interruptId;
    document.getElementById("hitl-message").textContent = message;
    
    const modal = document.getElementById("hitl-modal");
    const content = document.getElementById("hitl-modal-content");
    
    modal.classList.remove("hidden");
    setTimeout(() => {
        content.classList.add("show");
    }, 10);
}

function hideHitlModal() {
    const modal = document.getElementById("hitl-modal");
    const content = document.getElementById("hitl-modal-content");
    
    content.classList.remove("show");
    setTimeout(() => {
        modal.classList.add("hidden");
    }, 300);
}

async function handleHitlResponse(responseValue) {
    if (!activeSessionId || !currentInterruptId) return;
    
    hideHitlModal();
    logToTerminal(`Sending confirmation response: ${responseValue}...`);
    
    // Close existing stream if any
    if (eventSource) eventSource.close();
    
    logToTerminal("Resuming evaluation...", "info");
    // Reconnect to the stream with resume parameters
    connectStream(activeSessionId, activeUrl, true, responseValue, currentInterruptId);
}

// Fetch Submissions and Render Grid
async function fetchSubmissions() {
    const grid = document.getElementById("projects-grid");
    
    try {
        const response = await fetch("/api/submissions");
        if (!response.ok) throw new Error("Failed to fetch submissions");
        
        const projects = await response.json();
        grid.innerHTML = "";

        if (projects.length === 0) {
            grid.innerHTML = `
                <div class="col-span-full py-12 flex flex-col items-center justify-center text-gray-500 space-y-2">
                    <i class="fa-solid fa-folder-open text-3xl"></i>
                    <p class="text-sm">No evaluations found yet. Start one above!</p>
                </div>
            `;
            return;
        }

        // Check if our cookie session is already in the completed list
        const cookieSessionId = getCookie("grader_session_id");
        let isCookieSessionCompleted = false;

        projects.forEach(project => {
            const card = createProjectCard(project);
            grid.appendChild(card);

            // Check if cookie session is in this project's history
            if (cookieSessionId) {
                const found = project.history.some(h => h.session_id === cookieSessionId);
                if (found) {
                    isCookieSessionCompleted = true;
                }
            }
        });

        // If we have a cookie session, and it is NOT completed, and we are NOT currently streaming,
        // it means the session was interrupted (e.g. page refresh during run). We should reconnect.
        if (cookieSessionId && !isCookieSessionCompleted && !eventSource && !activeSessionId) {
            // We need the URL to reconnect. We can find the URL by looking at the "pending" state or
            // just asking the user, but we can also store the active URL in localStorage as a helper.
            const savedUrl = localStorage.getItem("active_url");
            if (savedUrl) {
                logToTerminal("Restoring interrupted session...", "warning");
                connectStream(cookieSessionId, savedUrl);
            }
        }

        // Update stats
        updateStats(projects);

    } catch (error) {
        logger("Error fetching submissions: " + error);
        grid.innerHTML = `
            <div class="col-span-full py-12 flex flex-col items-center justify-center text-red-400 space-y-2">
                <i class="fa-solid fa-triangle-exclamation text-3xl"></i>
                <p class="text-sm">Failed to load projects. Is Firestore running?</p>
            </div>
        `;
    }
}

function createProjectCard(project) {
    const repoName = parseRepoName(project.url);
    const card = document.createElement("div");
    card.className = "glass-card rounded-2xl p-6 flex flex-col justify-between space-y-6 relative overflow-hidden group";
    
    // Determine color based on score
    const score = project.latest_score;
    let scoreColorClass = "text-purple-400 text-glow-purple";
    if (score >= 85) scoreColorClass = "text-emerald-400 text-glow-green";
    else if (score < 60) scoreColorClass = "text-red-400 text-glow-red";

    // Format date
    const dateStr = new Date(project.latest_timestamp).toLocaleDateString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute:'2-digit'
    });

    const isPending = activeUrl === project.url;

    card.innerHTML = `
        <div class="space-y-4">
            <div class="flex items-start justify-between">
                <div class="space-y-1">
                    <h3 class="font-semibold text-lg text-white truncate max-w-[180px]" title="${repoName}">${repoName}</h3>
                    <a href="${project.url}" target="_blank" class="text-xs text-purple-400 hover:text-purple-300 flex items-center space-x-1">
                        <span class="truncate max-w-[150px]">${project.url}</span>
                        <i class="fa-solid fa-external-link text-[10px]"></i>
                    </a>
                </div>
                <div class="text-right">
                    <div class="text-2xl font-bold ${scoreColorClass}">${score}/95</div>
                    <span class="text-[10px] text-gray-500">${dateStr}</span>
                </div>
            </div>
        </div>

        <div class="space-y-3 pt-2 border-t border-white/5">
            <!-- History Toggle -->
            <button class="w-full py-1.5 px-3 rounded-lg bg-white/5 hover:bg-white/10 text-xs text-gray-400 flex items-center justify-between transition-colors"
                    onclick="toggleHistory(this)">
                <span>View History (${project.history.length})</span>
                <i class="fa-solid fa-chevron-down transition-transform duration-200"></i>
            </button>
            
            <!-- History List (Hidden by default) -->
            <div class="history-list hidden space-y-2 max-h-[120px] overflow-y-auto pr-1 text-[11px] text-gray-400 font-mono">
                ${project.history.map(run => {
                    const runDate = new Date(run.timestamp).toLocaleDateString(undefined, {month: 'short', day: 'numeric', hour: '2-digit', minute:'2-digit'});
                    return `
                        <div class="flex items-center justify-between py-1 border-b border-white/5">
                            <span>${runDate}</span>
                            <div class="flex items-center space-x-2">
                                <span class="font-semibold">${run.total_score}/95</span>
                                <button class="text-purple-400 hover:text-purple-300 transition-colors" onclick="openReport('${run.session_id}')" title="View Report">
                                    <i class="fa-solid fa-file-lines text-[10px]"></i>
                                </button>
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>

            <!-- Action Buttons -->
            <div class="grid grid-cols-2 gap-3">
                <button class="resubmit-btn py-2 px-4 bg-white/5 hover:bg-white/10 text-white font-medium rounded-xl text-xs transition-all flex items-center justify-center space-x-1.5 border border-white/10"
                        ${isPending ? "disabled" : ""}
                        onclick="handleResubmit('${project.url}', this)">
                    <i class="fa-solid fa-arrows-rotate text-[10px]"></i>
                    <span>${isPending ? "Evaluating..." : "Resubmit"}</span>
                </button>
                <button class="view-report-btn py-2 px-4 bg-purple-600/20 hover:bg-purple-600/35 text-purple-300 font-medium rounded-xl text-xs transition-all flex items-center justify-center space-x-1.5 border border-purple-500/20"
                        onclick="openReport('${project.history[0].session_id}')">
                    <i class="fa-solid fa-file-lines text-[10px]"></i>
                    <span>View Report</span>
                </button>
            </div>
        </div>
    `;
    
    return card;
}

function toggleHistory(button) {
    const card = button.closest(".glass-card");
    const list = card.querySelector(".history-list");
    const icon = button.querySelector("i");
    
    list.classList.toggle("hidden");
    icon.classList.toggle("rotate-180");
}

async function handleResubmit(url, button) {
    logger("Resubmitting: " + url);
    // Store active url in localStorage for restore helper
    localStorage.setItem("active_url", url);
    await startEvaluation(url);
}

// Helper: Parse Repo Name from URL
function parseRepoName(url) {
    if (!url) return "Unknown";
    // If it's a local path
    if (url.startsWith("/") || url.startsWith(".")) {
        return url.split("/").pop() || url;
    }
    // If it's a GitHub URL
    const match = url.match(/github\.com\/([^/]+\/[^/]+)/);
    return match ? match[1] : url;
}

// Helper: Get Cookie Value
function getCookie(name) {
    const value = `; ${document.cookie}`;
    const parts = value.split(`; ${name}=`);
    if (parts.length === 2) return parts.pop().split(';').shift();
    return null;
}

// Debug Logger
function logger(msg) {
    console.log("[Grader Dashboard] " + msg);
}

let activeReportMarkdown = "";

async function openReport(sessionId) {
    logger("Opening report for session: " + sessionId);
    
    // Show loading state in modal
    const modal = document.getElementById("report-modal");
    const content = document.getElementById("report-modal-content");
    const markdownContainer = document.getElementById("report-markdown-content");
    
    markdownContainer.innerHTML = `
        <div class="flex flex-col items-center justify-center py-12 space-y-3">
            <i class="fa-solid fa-circle-notch fa-spin text-2xl text-purple-400"></i>
            <p class="text-sm text-gray-400">Fetching report...</p>
        </div>
    `;
    
    // Reset copy button
    const copyBtnText = document.getElementById("copy-btn-text");
    copyBtnText.textContent = "Copy Markdown";
    copyBtnText.previousElementSibling.className = "fa-solid fa-copy text-xs";
    
    modal.classList.remove("hidden");
    setTimeout(() => {
        content.classList.add("show");
    }, 10);
    
    try {
        const response = await fetch(`/api/submissions/${sessionId}/report`);
        if (!response.ok) throw new Error("Failed to fetch report");
        
        const data = await response.json();
        activeReportMarkdown = data.markdown;
        
        // Render Markdown to HTML using Marked.js
        markdownContainer.innerHTML = marked.parse(activeReportMarkdown);
    } catch (error) {
        logger("Error loading report: " + error);
        markdownContainer.innerHTML = `
            <div class="text-red-400 text-center py-12 space-y-2">
                <i class="fa-solid fa-triangle-exclamation text-2xl"></i>
                <p>Failed to load report: ${error.message}</p>
            </div>
        `;
    }
}

function closeReport() {
    const modal = document.getElementById("report-modal");
    const content = document.getElementById("report-modal-content");
    
    content.classList.remove("show");
    setTimeout(() => {
        modal.classList.add("hidden");
    }, 300);
}

async function copyReportMarkdown() {
    if (!activeReportMarkdown) return;
    
    const copyBtnText = document.getElementById("copy-btn-text");
    const copyIcon = copyBtnText.previousElementSibling;
    
    try {
        await navigator.clipboard.writeText(activeReportMarkdown);
        copyBtnText.textContent = "Copied!";
        copyIcon.className = "fa-solid fa-check text-xs text-emerald-400";
        
        setTimeout(() => {
            copyBtnText.textContent = "Copy Markdown";
            copyIcon.className = "fa-solid fa-copy text-xs";
        }, 2000);
    } catch (err) {
        logger("Failed to copy: " + err);
        alert("Failed to copy to clipboard.");
    }
}

// Add event listeners for report modal close and copy
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("close-report-btn").addEventListener("click", closeReport);
    document.getElementById("copy-report-btn").addEventListener("click", copyReportMarkdown);
    
    // Also close modal if clicking outside the content
    document.getElementById("report-modal").addEventListener("click", (e) => {
        if (e.target === document.getElementById("report-modal")) {
            closeReport();
        }
    });
});

function updateStats(projects) {
    const totalProjects = projects.length;
    let sumScore = 0;
    let passedProjects = 0;
    let projectsWithScores = 0;

    projects.forEach(p => {
        if (p.latest_score !== undefined && p.latest_score !== null) {
            sumScore += p.latest_score;
            projectsWithScores++;
            if (p.latest_score >= 80) {
                passedProjects++;
            }
        }
    });

    const avgScore = projectsWithScores > 0 ? Math.round(sumScore / projectsWithScores) : 0;
    const passRate = totalProjects > 0 ? Math.round((passedProjects / totalProjects) * 100) : 0;

    document.getElementById("stat-total-projects").textContent = totalProjects;
    document.getElementById("stat-avg-score").textContent = avgScore > 0 ? `${avgScore}/95` : "-";
    document.getElementById("stat-pass-rate").textContent = totalProjects > 0 ? `${passRate}%` : "-";
}

function closeTerminal() {
    document.getElementById("active-run-panel").classList.add("hidden");
    document.getElementById("stats-panel").classList.remove("hidden");
}
