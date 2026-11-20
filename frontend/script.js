const API_BASE = "/api";
let dialogflowSessionId = null;
let dialogflowSending = false;
let selectedLanguage = "en";
let speechRecognition = null;
let speechRecognitionActive = false;
let voiceConversationEnabled = false;
let speakingUtterance = null;
let speakingArabicAudio = null;
let speakingArabicAudioUrl = null;
let conversationVersion = 0;
let speechRequestVersion = 0;

const speechLocales = { en: "en-IN", hi: "hi-IN", ar: "ar-SA" };
const greetingMessages = { en: "Hello", hi: "नमस्ते", ar: "مرحبا" };
const chatCopy = {
  en: {
    placeholder: "Type your message...",
    send: "Send",
    sending: "Sending...",
    voiceHelp: "Text chat is available. Voice depends on browser language support.",
    emptyReply: "No response was received from the assistant.",
    unavailable: "The assistant is unavailable. Please check whether the backend is running.",
    voiceUnavailable: "A voice for the selected language is unavailable. The reply is shown as text.",
  },
  hi: {
    placeholder: "अपना संदेश लिखें...",
    send: "भेजें",
    sending: "भेज रहे हैं...",
    voiceHelp: "टेक्स्ट चैट उपलब्ध है। वॉइस ब्राउज़र की भाषा सहायता पर निर्भर करती है।",
    emptyReply: "सहायक से कोई जवाब नहीं मिला।",
    unavailable: "सहायक उपलब्ध नहीं है। कृपया बैकएंड जांचें।",
    voiceUnavailable: "इस भाषा की आवाज़ इस ब्राउज़र में उपलब्ध नहीं है। जवाब टेक्स्ट में दिखाया गया है।",
  },
  ar: {
    placeholder: "اكتب رسالتك...",
    send: "إرسال",
    sending: "جارٍ الإرسال...",
    voiceHelp: "الدردشة النصية متاحة. يعتمد الصوت على دعم المتصفح للغة.",
    emptyReply: "لم يصل رد من المساعد.",
    unavailable: "المساعد غير متاح الآن. يرجى التحقق من الخادم.",
    voiceUnavailable: "لا يتوفر صوت عربي لتحويل النص إلى كلام في هذا المتصفح. سيظل الرد ظاهرًا كنص.",
    audioUnavailable: "تعذر إنشاء الصوت العربي. سيظل الرد ظاهرًا كنص.",
    audioPlaybackBlocked: "منع المتصفح تشغيل الصوت تلقائيًا. استخدم عناصر التحكم في الصوت ضمن الرد.",
    audioPlaybackFailed: "تعذر تشغيل الصوت العربي. سيظل الرد ظاهرًا كنص.",
    audioLoading: "جارٍ إنشاء الصوت العربي...",
  },
};

// Escape API values before inserting them into HTML
function escapeHTML(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;",
      })[char],
  );
}

// Format currency
function formatCurrency(amount) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(amount);
}

async function updateRenewalAction(path, successMessage) {
  const confirmationMessage = document.getElementById("confirmationMessage");
  try {
    const response = await fetch(`${API_BASE}${path}`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Unable to update renewal");
    }
    confirmationMessage.textContent = successMessage;
  } catch (error) {
    confirmationMessage.textContent = error.message;
  }
}

async function loadConfirmationFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const demoToken = params.get("demo_payment");
  if (demoToken) {
    await loadDemoPaymentPage(demoToken, params.get("lang"));
    return;
  }
  const token = params.get("token");
  if (!token) {
    return;
  }

  const confirmationMessage = document.getElementById("confirmationMessage");
  confirmationMessage.textContent = "Validating renewal confirmation...";

  try {
    const response = await fetch(
      `${API_BASE}/confirmation/${encodeURIComponent(token)}`,
    );
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Unable to validate confirmation link");
    }

    confirmationMessage.replaceChildren();

    if (data.status === "confirmed") {
      confirmationMessage.textContent = "Renewal confirmed successfully.";
      return;
    }

    const customerName = data.customer?.full_name || "Customer";
    const productName = data.product?.product_name || "Product";
    const amount = formatCurrency(data.renewal.amount);
    confirmationMessage.append(
      `Confirm renewal for ${customerName}: ${productName}, ${amount} (${data.renewal.billing_cycle}).`,
    );

    const confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.textContent = "Confirm Renewal";
    confirmButton.addEventListener("click", async () => {
      confirmButton.disabled = true;
      try {
        const confirmResponse = await fetch(
          `${API_BASE}/confirmation/${encodeURIComponent(token)}/confirm`,
          { method: "POST" },
        );
        const confirmData = await confirmResponse.json();
        if (!confirmResponse.ok) {
          throw new Error(
            confirmData.detail || "Unable to confirm renewal",
          );
        }
        confirmationMessage.textContent =
          "Renewal confirmed successfully.";
      } catch (error) {
        confirmationMessage.textContent = error.message;
      }
    });
    confirmationMessage.appendChild(document.createElement("br"));
    confirmationMessage.appendChild(confirmButton);
  } catch (error) {
    confirmationMessage.textContent = error.message;
  }
}

async function loadDemoPaymentPage(token, requestedLanguage) {
  const section = document.getElementById("demoPaymentSection");
  const details = document.getElementById("demoPaymentDetails");
  const language = ["en", "hi", "ar"].includes(requestedLanguage)
    ? requestedLanguage
    : "en";
  const copy = {
    en: {
      label: "TESTING ONLY",
      title: "Payment Details",
      notice: "This page does not accept payments. SMS was not sent, and payment has not been received.",
      invoice: "Invoice",
      amount: "Amount",
      due: "Due date",
      invoiceStatus: "Invoice status",
      paymentStatus: "Payment status",
      smsStatus: "SMS status",
      destination: "Masked destination",
      pending: "Payment has not been received",
      notSent: "SMS was not sent",
      loading: "Loading payment details...",
      loadError: "Payment details are unavailable. Please try again later.",
      statuses: { pending: "Pending", paid: "Paid", overdue: "Overdue" },
    },
    hi: {
      label: "केवल परीक्षण हेतु",
      title: "भुगतान विवरण",
      notice: "यह पृष्ठ भुगतान स्वीकार नहीं करता। SMS नहीं भेजा गया है और भुगतान प्राप्त नहीं हुआ है।",
      invoice: "इनवॉइस",
      amount: "राशि",
      due: "देय तिथि",
      invoiceStatus: "इनवॉइस स्थिति",
      paymentStatus: "भुगतान स्थिति",
      smsStatus: "SMS स्थिति",
      destination: "छिपाया गया गंतव्य",
      pending: "भुगतान प्राप्त नहीं हुआ है",
      notSent: "SMS नहीं भेजा गया",
      loading: "भुगतान विवरण लोड हो रहे हैं...",
      loadError: "भुगतान विवरण उपलब्ध नहीं हैं। कृपया बाद में फिर प्रयास करें।",
      statuses: { pending: "लंबित", paid: "भुगतान किया गया", overdue: "बकाया" },
    },
    ar: {
      label: "للاختبار فقط",
      title: "تفاصيل الدفع",
      notice: "هذه الصفحة لا تقبل المدفوعات. لم تُرسل رسالة نصية ولم يتم استلام أي دفعة.",
      invoice: "الفاتورة",
      amount: "المبلغ",
      due: "تاريخ الاستحقاق",
      invoiceStatus: "حالة الفاتورة",
      paymentStatus: "حالة الدفع",
      smsStatus: "حالة الرسالة",
      destination: "الوجهة بعد إخفاء الرقم",
      pending: "لم يتم استلام أي دفعة",
      notSent: "لم تُرسل رسالة نصية",
      loading: "جارٍ تحميل تفاصيل الدفع...",
      loadError: "تفاصيل الدفع غير متاحة. يرجى المحاولة مرة أخرى لاحقًا.",
      statuses: { pending: "قيد الانتظار", paid: "مدفوعة", overdue: "متأخرة" },
    },
  }[language];
  selectedLanguage = language;
  document.getElementById("languageSelect").value = language;
  setChatDirection();
  section.classList.remove("hidden");
  section.dir = language === "ar" ? "rtl" : "ltr";
  section.querySelector(".eyebrow").textContent = copy.label;
  document.getElementById("demoPaymentTitle").textContent = copy.title;
  document.getElementById("demoPaymentNotice").textContent = copy.notice;
  details.textContent = copy.loading;

  try {
    const response = await fetch(
      `${API_BASE}/demo/payment/${encodeURIComponent(token)}`,
    );
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Unable to load payment details");
    }

    details.replaceChildren();
    const rows = [
      [copy.invoice, data.invoice_number],
      [copy.amount, formatCurrency(data.amount)],
      [copy.due, data.due_date],
      [
        copy.invoiceStatus,
        copy.statuses[String(data.invoice_status).toLowerCase()] ||
          data.invoice_status,
      ],
      [copy.paymentStatus, copy.pending],
      [copy.smsStatus, copy.notSent],
      [copy.destination, data.destination_masked],
    ];
    rows.forEach(([label, value]) => {
      const row = document.createElement("div");
      row.className = "detail-row";
      const labelElement = document.createElement("span");
      labelElement.textContent = label;
      const valueElement = document.createElement("span");
      valueElement.textContent = String(value ?? "");
      row.append(labelElement, valueElement);
      details.appendChild(row);
    });
  } catch (error) {
    console.error("Payment detail loading failed:", error);
    details.textContent = copy.loadError;
  }
}

function formatDemoStatus(status) {
  const labels = {
    draft: "Pending",
    demo_completed: "Campaign completed — no calls placed",
    demo_call_initiated: "Call attempt recorded — no phone call placed",
    demo_sms_simulated: "Testing payment link generated — SMS was not sent",
    demo_followup_scheduled: "Follow-up Scheduled",
    queued: "Pending",
  };
  return labels[status] || String(status || "Pending").replace(/_/g, " ");
}

function renderDemoActivity(container, activities) {
  container.replaceChildren();
  if (!activities.length) {
    const empty = document.createElement("p");
    empty.className = "demo-empty";
    empty.textContent = "No recent campaign activity recorded.";
    container.appendChild(empty);
    return;
  }

  activities.forEach((activity) => {
    const card = document.createElement("article");
    card.className = "demo-activity-card";
    const title = document.createElement("h4");
    title.textContent =
      `${activity.customer_id} · ${activity.invoice_number}`;
    const status = document.createElement("p");
    status.className = "demo-activity-status";
    status.textContent = formatDemoStatus(activity.status);
    card.append(title, status);
    if (activity.amount !== null && activity.amount !== undefined) {
      const amount = document.createElement("p");
      amount.textContent = `Amount: ${formatCurrency(activity.amount)}`;
      card.appendChild(amount);
    }
    if (activity.destination_masked) {
      const destination = document.createElement("p");
      destination.textContent =
        `Masked destination: ${activity.destination_masked}`;
      card.appendChild(destination);
    }
    if (activity.preferred_payment_date) {
      const date = document.createElement("p");
      date.textContent =
        `Preferred payment date: ${activity.preferred_payment_date}`;
      card.appendChild(date);
    }
    if (activity.payment_status) {
      const payment = document.createElement("p");
      payment.textContent = `Payment status: ${activity.payment_status}`;
      card.appendChild(payment);
    }
    if (activity.sms_status === "simulated") {
      const sms = document.createElement("p");
      sms.textContent = "SMS status: Not sent.";
      card.appendChild(sms);
    }
    container.appendChild(card);
  });
}

async function demoCampaignRequest(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail || "Unable to update demo campaign");
  }
  return data;
}

async function refreshDemoCampaigns() {
  const campaignList = document.getElementById("demoCampaignList");
  const activityList = document.getElementById("demoActivityList");
  const message = document.getElementById("demoCampaignMessage");
  try {
    const data = await demoCampaignRequest("/campaigns");
    campaignList.replaceChildren();
    if (!data.campaigns.length) {
      const empty = document.createElement("p");
      empty.className = "demo-empty";
      empty.textContent = "No campaigns yet. Create a campaign to begin.";
      campaignList.appendChild(empty);
    }
    data.campaigns.forEach((campaign) => {
      const card = document.createElement("article");
      card.className = "demo-campaign-card";
      const heading = document.createElement("div");
      heading.className = "demo-campaign-heading";
      const title = document.createElement("h3");
      title.textContent = campaign.campaign_name;
      const status = document.createElement("span");
      status.className = "demo-status-badge";
      status.textContent = formatDemoStatus(
        campaign.display_status || campaign.status,
      );
      heading.append(title, status);
      card.appendChild(heading);
      if (campaign.status === "draft") {
        const runButton = document.createElement("button");
        runButton.type = "button";
        runButton.textContent = "Run Campaign";
        runButton.addEventListener("click", () => {
          runDemoCampaign(campaign.id, runButton);
        });
        card.appendChild(runButton);
      }
      const activity = document.createElement("div");
      activity.className = "demo-campaign-activity";
      renderDemoActivity(activity, campaign.activity || []);
      card.appendChild(activity);
      campaignList.appendChild(card);
    });
    renderDemoActivity(activityList, data.demo_activity || []);
  } catch (error) {
    message.textContent = error.message;
  }
}

async function loadDemoCampaignManagement() {
  try {
    const response = await fetch(`${API_BASE}/health`);
    const data = await response.json();
    if (!response.ok || !data.demo_mode) {
      return;
    }
    document.getElementById("demoCampaignSection").classList.remove("hidden");
    await refreshDemoCampaigns();
  } catch (error) {
    console.warn("Unable to load demo campaign controls:", error);
  }
}

async function runDemoCampaign(campaignId, button) {
  const message = document.getElementById("demoCampaignMessage");
  button.disabled = true;
  try {
    const data = await demoCampaignRequest(
      `/campaigns/${encodeURIComponent(campaignId)}/execute`,
      { method: "POST" },
    );
    const started = data.results.filter(
      (result) => result.status === "demo_call_initiated",
    ).length;
    message.textContent = started
      ? `${started} call attempt(s) recorded. No phone call was placed.`
      : "No new eligible invoices were found; no call was placed.";
    await refreshDemoCampaigns();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

// Load products from FastAPI
async function loadProducts() {
  const productsGrid = document.getElementById("productsGrid");

  try {
    productsGrid.innerHTML = '<p class="loading">Loading products...</p>';

    const response = await fetch(`${API_BASE}/products`);

    if (!response.ok) {
      throw new Error("Unable to fetch products");
    }

    const data = await response.json();

    if (!data.products || data.products.length === 0) {
      productsGrid.innerHTML = "<p>No active products available.</p>";
      return;
    }

    productsGrid.innerHTML = data.products
      .map(
        (product) => `
            <article class="product-card">
                <div class="product-icon">
                    ${escapeHTML(product.product_id.split("-").pop())}
                </div>

                <h3>${escapeHTML(product.product_name)}</h3>

                <p>${escapeHTML(product.description)}</p>

                <div class="product-price">
                    ${formatCurrency(product.renewal_amount)}
                    <span>/ ${escapeHTML(product.billing_cycle)}</span>
                </div>
            </article>
        `,
      )
      .join("");
  } catch (error) {
    console.error("Product loading error:", error);

    productsGrid.innerHTML = `
            <p>Unable to load products. Please check whether the backend is running.</p>
        `;
  }
}

// Load customer renewal details
async function loadCustomerRenewal(customerId) {
  const renewalDetails = document.getElementById("renewalDetails");
  const lookupBtn = document.getElementById("lookupBtn");

  try {
    lookupBtn.disabled = true;
    lookupBtn.textContent = "Loading...";

    renewalDetails.classList.remove("hidden");
    renewalDetails.innerHTML = "<p>Fetching renewal details...</p>";

    const response = await fetch(
      `${API_BASE}/customer/${encodeURIComponent(customerId)}/renewal`,
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Customer renewal not found");
    }

    const customer = data.customer;
    const renewal = data.renewal;
    const product = data.product;

    if (!product) {
      throw new Error("Assigned product details are unavailable");
    }

    renewalDetails.innerHTML = `
            <h3>Renewal Details</h3>

            <div class="detail-row">
                <span>Customer Name</span>
                <span>${escapeHTML(customer.full_name)}</span>
            </div>

            <div class="detail-row">
                <span>Customer ID</span>
                <span>${escapeHTML(customer.customer_id)}</span>
            </div>

            <div class="detail-row">
                <span>Product</span>
                <span>${escapeHTML(product.product_name)}</span>
            </div>

            <div class="detail-row">
                <span>Description</span>
                <span>${escapeHTML(product.description)}</span>
            </div>

            <div class="detail-row">
                <span>Renewal Amount</span>
                <span>${formatCurrency(renewal.renewal_amount)}</span>
            </div>

            <div class="detail-row">
                <span>Billing Cycle</span>
                <span>${escapeHTML(renewal.billing_cycle)}</span>
            </div>

            <div class="detail-row">
                <span>Renewal Status</span>
                <span>${escapeHTML(renewal.renewal_status)}</span>
            </div>
            <div class="renewal-actions">
                <button type="button" id="agreeRenewalBtn">Agree to Renew</button>
                <button type="button" id="linkRenewalBtn">Generate Confirmation Link</button>
            </div>
        `;

    document
      .getElementById("agreeRenewalBtn")
      .addEventListener("click", async () => {
        await updateRenewalAction(
          `/renewal/${encodeURIComponent(renewal.id)}/agree`,
          "Renewal agreed successfully",
        );
      });

    document
      .getElementById("linkRenewalBtn")
      .addEventListener("click", async () => {
        const linkResponse = await fetch(
          `${API_BASE}/renewal/${encodeURIComponent(renewal.id)}/confirmation-link`,
          { method: "POST" },
        );
        const linkData = await linkResponse.json();
        if (!linkResponse.ok) {
          throw new Error(linkData.detail || "Unable to generate confirmation link");
        }

        document.getElementById("confirmationMessage").replaceChildren();
        const link = document.createElement("a");
        link.href = linkData.confirmation_link;
        link.textContent = "Open renewal confirmation";
        link.target = "_blank";
        link.rel = "noopener";
        document.getElementById("confirmationMessage").appendChild(link);
      });
  } catch (error) {
    renewalDetails.innerHTML = `
            <p>${escapeHTML(error.message)}</p>
        `;
  } finally {
    lookupBtn.disabled = false;
    lookupBtn.textContent = "View Renewal";
  }
}

// Customer lookup form
document.getElementById("customerForm").addEventListener("submit", (event) => {
  event.preventDefault();

  const customerId = document.getElementById("customerId").value.trim();

  if (!customerId) {
    return;
  }

  loadCustomerRenewal(customerId);
});

// Initial page loading
document.addEventListener("DOMContentLoaded", () => {
  loadProducts();
  loadConfirmationFromUrl();
  loadDemoCampaignManagement();
  setChatDirection();
  const chatMessages = document.getElementById("chatMessages");
  chatMessages.replaceChildren();
  sendChatMessage(greetingMessages[selectedLanguage]);
});

function addChatMessage(text, sender) {
  const chatMessages = document.getElementById("chatMessages");
  const message = document.createElement("div");
  message.className =
    sender === "user" ? "message user-message" : "message bot-message";
  const segments = String(text ?? "").split(/(https?:\/\/[^\s]+)/g);
  segments.forEach((segment) => {
    if (/^https?:\/\//i.test(segment)) {
      try {
        const url = new URL(segment);
        if (url.protocol === "http:" || url.protocol === "https:") {
          const link = document.createElement("a");
          link.href = url.href;
          link.textContent = segment;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          message.appendChild(link);
          return;
        }
      } catch (error) {
        console.warn("Unable to render a chat link:", error);
      }
    }
    message.appendChild(document.createTextNode(segment));
  });

  if (selectedLanguage === "ar") {
    message.dir = "rtl";
  } else {
    message.dir = "ltr";
  }

  chatMessages.appendChild(message);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return message;
}

function setChatDirection() {
  const chatMessages = document.getElementById("chatMessages");
  const isArabic = selectedLanguage === "ar";
  chatMessages.dir = isArabic ? "rtl" : "ltr";
  document.documentElement.lang = selectedLanguage;

  document.querySelectorAll(".message").forEach((message) => {
    message.dir = isArabic ? "rtl" : "ltr";
  });

  const input = document.getElementById("chatInput");
  input.dir = isArabic ? "rtl" : "ltr";
  input.placeholder = chatCopy[selectedLanguage].placeholder;
  document.getElementById("sendBtn").textContent =
    chatCopy[selectedLanguage].send;
  document.querySelector(".chat-note").textContent =
    chatCopy[selectedLanguage].voiceHelp;
}

function updateVoiceStatus(message) {
  const translations = {
    "Speaking...": { hi: "बोल रहा है...", ar: "جارٍ التحدث..." },
    "Listening...": { hi: "सुन रहा है...", ar: "جارٍ الاستماع..." },
    "Text-to-speech is not supported by this browser.": {
      hi: "इस ब्राउज़र में टेक्स्ट-टू-स्पीच उपलब्ध नहीं है।",
      ar: "تحويل النص إلى كلام غير مدعوم في هذا المتصفح.",
    },
    "Text and browser voice are available.": {
      hi: "टेक्स्ट और ब्राउज़र वॉइस उपलब्ध हैं।",
      ar: "النص والصوت عبر المتصفح متاحان.",
    },
    "A voice for the selected language is unavailable. The reply is shown as text.": {
      hi: chatCopy.hi.voiceUnavailable,
      ar: chatCopy.ar.voiceUnavailable,
    },
    "Arabic audio is unavailable. The text response is still available.": {
      ar: chatCopy.ar.audioUnavailable,
    },
    "Automatic Arabic audio playback was blocked. Use the audio controls in the reply.": {
      ar: chatCopy.ar.audioPlaybackBlocked,
    },
    "Arabic audio could not be played. The text response is still available.": {
      ar: chatCopy.ar.audioPlaybackFailed,
    },
    "Arabic audio played.": {
      ar: "تم تشغيل الصوت العربي.",
    },
    "Generating Arabic audio...": {
      ar: chatCopy.ar.audioLoading,
    },
    "Speech recognition is not supported by this browser.": {
      hi: "इस ब्राउज़र में वाक् पहचान उपलब्ध नहीं है।",
      ar: "التعرف على الكلام غير مدعوم في هذا المتصفح.",
    },
    "Microphone permission was denied.": {
      hi: "माइक्रोफ़ोन की अनुमति अस्वीकार की गई।",
      ar: "تم رفض إذن الميكروفون.",
    },
    "Speech was not recognized. Please try again or type your message.": {
      hi: "आवाज़ समझ में नहीं आई। फिर से बोलें या संदेश टाइप करें।",
      ar: "لم يتم التعرف على الكلام. حاول مجددًا أو اكتب رسالتك.",
    },
    "Unable to start the microphone. Try again or type.": {
      hi: "माइक्रोफ़ोन शुरू नहीं हो सका। फिर से कोशिश करें या टाइप करें।",
      ar: "تعذر تشغيل الميكروفون. حاول مجددًا أو اكتب رسالتك.",
    },
    "Audio playback failed. You can continue by text.": {
      hi: "ऑडियो नहीं चल सका। आप टेक्स्ट से जारी रख सकते हैं।",
      ar: "تعذر تشغيل الصوت. يمكنك المتابعة عبر النص.",
    },
    "Voice conversation stopped. You can continue by text.": {
      hi: "वॉइस बातचीत रुक गई। आप टेक्स्ट से जारी रख सकते हैं।",
      ar: "تم إيقاف المحادثة الصوتية. يمكنك المتابعة عبر النص.",
    },
  };
  document.querySelector(".chat-note").textContent =
    translations[message]?.[selectedLanguage] ||
    (selectedLanguage === "en" ? message : chatCopy[selectedLanguage].voiceUnavailable);
}

function findSpeechVoice(language, voices) {
  const normalizedLanguage = language.toLowerCase();
  const matchingVoices = voices.filter((voice) => {
    if (typeof voice.lang !== "string") {
      return false;
    }
    const voiceLanguage = voice.lang.toLowerCase().replace("_", "-");
    return (
      voiceLanguage === normalizedLanguage ||
      voiceLanguage.startsWith(`${normalizedLanguage}-`)
    );
  });
  if (language === "en") {
    return (
      matchingVoices.find((voice) =>
        voice.lang.toLowerCase().replace("_", "-").startsWith("en-in"),
      ) || matchingVoices[0]
    );
  }
  if (language === "ar") {
    return (
      matchingVoices.find(
        (voice) => voice.lang.toLowerCase().replace("_", "-") === "ar-sa",
      ) || matchingVoices[0]
    );
  }
  return matchingVoices[0];
}

function waitForSpeechVoice(language) {
  const synthesis = window.speechSynthesis;
  const availableVoice = findSpeechVoice(language, synthesis.getVoices());
  if (availableVoice) {
    return Promise.resolve(availableVoice);
  }

  return new Promise((resolve) => {
    const finish = (voice) => {
      window.clearTimeout(timeoutId);
      synthesis.removeEventListener("voiceschanged", onVoicesChanged);
      resolve(voice);
    };
    const onVoicesChanged = () => {
      const voice = findSpeechVoice(language, synthesis.getVoices());
      if (voice) {
        finish(voice);
      }
    };
    const timeoutId = window.setTimeout(() => finish(undefined), 3000);
    synthesis.addEventListener("voiceschanged", onVoicesChanged);
    onVoicesChanged();
  });
}

async function speakChatReply(text) {
  cancelArabicAudio();
  if (!("speechSynthesis" in window)) {
    updateVoiceStatus("Text-to-speech is not supported by this browser.");
    return;
  }

  const language = selectedLanguage;
  const requestVersion = ++speechRequestVersion;
  const synthesis = window.speechSynthesis;
  synthesis.cancel();
  speakingUtterance = null;
  const voice = await waitForSpeechVoice(language);
  if (requestVersion !== speechRequestVersion) {
    return;
  }
  if (!voice) {
    updateVoiceStatus(
      "A voice for the selected language is unavailable. The reply is shown as text.",
    );
    voiceConversationEnabled = false;
    document.getElementById("voiceBtn").textContent = "🎤";
    return;
  }

  speakingUtterance = new SpeechSynthesisUtterance(text);
  speakingUtterance.lang =
    language === "ar" && voice.lang
      ? voice.lang.replace("_", "-")
      : speechLocales[language];
  speakingUtterance.voice = voice;
  speakingUtterance.onstart = () => {
    if (requestVersion === speechRequestVersion) {
      updateVoiceStatus("Speaking...");
    }
  };
  speakingUtterance.onend = () => {
    if (requestVersion !== speechRequestVersion) {
      return;
    }
    speakingUtterance = null;
    updateVoiceStatus("Text and browser voice are available.");
    if (voiceConversationEnabled) {
      startListening();
    }
  };
  speakingUtterance.onerror = () => {
    if (requestVersion !== speechRequestVersion) {
      return;
    }
    speakingUtterance = null;
    voiceConversationEnabled = false;
    if (speechRecognition) {
      speechRecognition.stop();
    }
    document.getElementById("voiceBtn").textContent = "🎤";
    updateVoiceStatus("Audio playback failed. You can continue by text.");
  };
  synthesis.speak(speakingUtterance);
}

function cancelArabicAudio() {
  const audio = speakingArabicAudio;
  const audioUrl = speakingArabicAudioUrl;
  speakingArabicAudio = null;
  speakingArabicAudioUrl = null;
  if (audio) {
    audio.onended = null;
    audio.onerror = null;
    audio.onplay = null;
    audio.pause();
    audio.remove();
  }
  if (audioUrl) {
    URL.revokeObjectURL(audioUrl);
  }
}

async function playArabicAudio(audioBase64, replyMessage) {
  const requestVersion = ++speechRequestVersion;
  if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
    speakingUtterance = null;
  }
  cancelArabicAudio();
  if (!audioBase64) {
    updateVoiceStatus("Arabic audio is unavailable. The text response is still available.");
    return;
  }

  let audio;
  let audioUrl;
  try {
    const binaryAudio = atob(audioBase64);
    const audioBytes = new Uint8Array(binaryAudio.length);
    for (let index = 0; index < binaryAudio.length; index += 1) {
      audioBytes[index] = binaryAudio.charCodeAt(index);
    }
    const audioBlob = new Blob([audioBytes], { type: "audio/mpeg" });
    if (!audioBlob.size) {
      throw new Error("Empty Arabic audio");
    }
    audioUrl = URL.createObjectURL(audioBlob);
    audio = new Audio(audioUrl);
    speakingArabicAudio = audio;
    speakingArabicAudioUrl = audioUrl;
    audio.onplay = () => {
      if (requestVersion === speechRequestVersion) {
        updateVoiceStatus("Speaking...");
      }
    };
    audio.onended = () => {
      if (requestVersion !== speechRequestVersion) {
        return;
      }
      cancelArabicAudio();
      updateVoiceStatus("Arabic audio played.");
      if (voiceConversationEnabled) {
        startListening();
      }
    };
    audio.onerror = () => {
      if (requestVersion !== speechRequestVersion) {
        return;
      }
      cancelArabicAudio();
      voiceConversationEnabled = false;
      if (speechRecognition) {
        speechRecognition.stop();
      }
      document.getElementById("voiceBtn").textContent = "🎤";
      updateVoiceStatus("Arabic audio could not be played. The text response is still available.");
    };
    try {
      await audio.play();
    } catch (error) {
      if (requestVersion !== speechRequestVersion) {
        return;
      }
      if (error.name === "NotAllowedError") {
        audio.controls = true;
        audio.setAttribute("aria-label", "Arabic assistant response audio");
        replyMessage.appendChild(audio);
        updateVoiceStatus(
          "Automatic Arabic audio playback was blocked. Use the audio controls in the reply.",
        );
        return;
      }
      throw error;
    }
  } catch (error) {
    if (requestVersion !== speechRequestVersion) {
      return;
    }
    cancelArabicAudio();
    voiceConversationEnabled = false;
    if (speechRecognition) {
      speechRecognition.stop();
    }
    document.getElementById("voiceBtn").textContent = "🎤";
    updateVoiceStatus("Arabic audio could not be played. The text response is still available.");
  }
}

function speakChatReplyForLanguage(text, language, response, replyMessage) {
  if (language === "ar") {
    void playArabicAudio(response && response.audio_base64, replyMessage);
    return;
  }
  speakChatReply(text);
}

function startListening() {
  const Recognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    voiceConversationEnabled = false;
    updateVoiceStatus("Speech recognition is not supported by this browser.");
    return;
  }

  if (!speechRecognition) {
    speechRecognition = new Recognition();
    speechRecognition.continuous = false;
    speechRecognition.interimResults = false;
    speechRecognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript.trim();
      if (transcript) {
        submitChatMessage(transcript);
      }
    };
    speechRecognition.onerror = (event) => {
      const message =
        event.error === "not-allowed"
          ? "Microphone permission was denied."
          : "Speech was not recognized. Please try again or type your message.";
      updateVoiceStatus(message);
      if (event.error === "not-allowed") {
        voiceConversationEnabled = false;
        document.getElementById("voiceBtn").textContent = "🎤";
      }
    };
    speechRecognition.onend = () => {
      speechRecognitionActive = false;
      if (!voiceConversationEnabled) {
        document.getElementById("voiceBtn").textContent = "🎤";
      }
    };
    speechRecognition.onstart = () => {
      speechRecognitionActive = true;
    };
  }

  speechRecognition.lang = speechLocales[selectedLanguage];
  if (speechRecognitionActive) {
    return;
  }
  try {
    speechRecognition.start();
    document.getElementById("voiceBtn").textContent = "■";
    updateVoiceStatus("Listening...");
  } catch (error) {
    voiceConversationEnabled = false;
    document.getElementById("voiceBtn").textContent = "🎤";
    updateVoiceStatus(
      error.name === "NotAllowedError"
        ? "Microphone permission was denied."
        : "Unable to start the microphone. Try again or type.",
    );
  }
}

async function sendChatMessage(message) {
  if (dialogflowSending) {
    return;
  }

  const requestLanguage = selectedLanguage;
  const requestVersion = conversationVersion;
  dialogflowSending = true;
  const sendBtn = document.getElementById("sendBtn");
  const chatInput = document.getElementById("chatInput");
  const languageSelect = document.getElementById("languageSelect");
  const voiceBtn = document.getElementById("voiceBtn");
  sendBtn.disabled = true;
  chatInput.disabled = true;
  languageSelect.disabled = true;
  voiceBtn.disabled = true;
  sendBtn.textContent = chatCopy[requestLanguage].sending;
  if (requestLanguage === "ar") {
    updateVoiceStatus("Generating Arabic audio...");
  }

  const loadingMessage = addChatMessage("...", "bot");

  try {
    const response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        session_id: dialogflowSessionId,
        language_code: requestLanguage,
      }),
    });

    const data = await response.json();
    loadingMessage.remove();
    if (requestVersion !== conversationVersion) {
      return;
    }

    if (!response.ok) {
      throw new Error(data.detail || "Unable to reach the assistant");
    }

    dialogflowSessionId = data.session_id;
    if (data.reply) {
      const replyMessage = addChatMessage(data.reply, "bot");
      speakChatReplyForLanguage(
        data.reply,
        requestLanguage,
        data,
        replyMessage,
      );
    } else {
      const fallback = chatCopy[requestLanguage].emptyReply;
      const replyMessage = addChatMessage(fallback, "bot");
      speakChatReplyForLanguage(fallback, requestLanguage, null, replyMessage);
    }
  } catch (error) {
    loadingMessage.remove();
    if (requestVersion !== conversationVersion) {
      return;
    }
    const fallback = chatCopy[requestLanguage].unavailable;
    const replyMessage = addChatMessage(fallback, "bot");
    speakChatReplyForLanguage(fallback, requestLanguage, null, replyMessage);
  } finally {
    dialogflowSending = false;
    sendBtn.disabled = false;
    chatInput.disabled = false;
    languageSelect.disabled = false;
    voiceBtn.disabled = false;
    sendBtn.textContent = chatCopy[requestLanguage].send;
    if (requestLanguage === selectedLanguage) {
      chatInput.focus();
    }
  }
}

function submitChatMessage(message) {
  if (dialogflowSending) {
    return;
  }
  if (speechRecognition && speechRecognitionActive) {
    speechRecognition.stop();
  }
  const chatInput = document.getElementById("chatInput");
  chatInput.value = "";
  addChatMessage(message, "user");
  sendChatMessage(message);
}

document.getElementById("chatForm").addEventListener("submit", (event) => {
  event.preventDefault();

  const chatInput = document.getElementById("chatInput");
  const message = chatInput.value.trim();
  if (!message) {
    return;
  }

  submitChatMessage(message);
});

document.getElementById("languageSelect").addEventListener("change", (event) => {
  voiceConversationEnabled = false;
  if (speechRecognition) {
    speechRecognition.stop();
  }
  if ("speechSynthesis" in window) {
    speechRequestVersion += 1;
    speakingUtterance = null;
    window.speechSynthesis.cancel();
  }
  cancelArabicAudio();
  selectedLanguage = event.target.value;
  conversationVersion += 1;
  speechRequestVersion += 1;
  dialogflowSessionId = null;
  setChatDirection();

  const chatMessages = document.getElementById("chatMessages");
  chatMessages.replaceChildren();
  document.getElementById("chatInput").value = "";
  setChatDirection();
  sendChatMessage(greetingMessages[selectedLanguage]);
});

document.getElementById("voiceBtn").addEventListener("click", () => {
  if (voiceConversationEnabled) {
    voiceConversationEnabled = false;
    if (speechRecognition) {
      speechRecognition.stop();
    }
    if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    cancelArabicAudio();
    document.getElementById("voiceBtn").textContent = "🎤";
    updateVoiceStatus("Voice conversation stopped. You can continue by text.");
    return;
  }

  voiceConversationEnabled = true;
  startListening();
});

document.getElementById("demoCampaignForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const nameInput = document.getElementById("demoCampaignName");
  const createButton = document.getElementById("createDemoCampaignBtn");
  const message = document.getElementById("demoCampaignMessage");
  const campaignName = nameInput.value.trim();
  if (!campaignName) {
    message.textContent = "Enter a campaign name.";
    return;
  }
  createButton.disabled = true;
  try {
    await demoCampaignRequest("/campaigns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ campaign_name: campaignName }),
    });
    message.textContent =
      "Campaign created. Running it records call attempts only; no phone call will be placed.";
    await refreshDemoCampaigns();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    createButton.disabled = false;
  }
});

document
  .getElementById("refreshDemoCampaignsBtn")
  .addEventListener("click", refreshDemoCampaigns);
