let auto_extract_flag = false;
let leads = [];
const leads_lnglat = new Set();
const collect_email = false; // Raw Maps first; website lookup is an explicit dashboard action.

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function updateLeadCounter() {
  const downloadBtn = document.getElementById("extension_gms_download_btn");
  const leadsInfo = document.getElementById("extension_gms_leads_info");
  if (downloadBtn) downloadBtn.innerText = `Export Leads (${leads.length})`;
  if (leadsInfo) leadsInfo.innerHTML = `Leads: ${leads.length}`;
}

function getLatLngFromUrl() {
  const identity = getListingIdentity(window.location.href);
  return {
    latitude: identity.latitude,
    longitude: identity.longitude
  };
}

function getTextBySelector(selector) {
  return document.querySelector(selector)?.textContent?.trim() || "";
}

function getAttrBySelector(selector, attr) {
  return document.querySelector(selector)?.getAttribute(attr)?.trim() || "";
}

function getPlaceIdFromUrl() {
  return getListingIdentity(window.location.href).placeID;
}

function getListingIdentity(href) {
  const url = new URL(href, window.location.href);
  const placeMatch = url.href.match(/!19s([^!?#&]+)/) ||
    url.href.match(/!1s(ChIJ[A-Za-z0-9_-]+)/);
  let placeID = url.searchParams.get("query_place_id") || "";
  if (placeMatch) {
    try { placeID = decodeURIComponent(placeMatch[1]); } catch (_) { /* keep query ID */ }
  }
  const cidMatch = url.href.match(/!1s0x[0-9a-f]+:(0x[0-9a-f]+)/i);
  let cID = url.searchParams.get("cid") || "";
  if (cidMatch) {
    try { cID = BigInt(cidMatch[1]).toString(); } catch (_) { /* leave unknown */ }
  }
  return {
    placeID,
    cID,
    latitude: url.href.match(/!3d(-?\d+(?:\.\d+)?)/)?.[1] || "",
    longitude: url.href.match(/!4d(-?\d+(?:\.\d+)?)/)?.[1] || ""
  };
}

function storeLead(row, preferIncoming = false) {
  if (!row.name || /^(results|google maps)$/i.test(row.name)) return;
  const key = row.placeID || row.cID || `${row.name}|${row.address || ""}`;
  const existing = leads.find(item =>
    (item.placeID || item.cID || `${item.name}|${item.address || ""}`) === key);
  if (existing) {
    for (const [field, value] of Object.entries(row)) {
      if (value !== "" && value != null && (preferIncoming || !existing[field])) {
        existing[field] = value;
      }
    }
  } else {
    leads.push(row);
  }
  leads_lnglat.add(key);
}

function collectVisibleSearchResults() {
  // Search XHRs can be missed or change format. Read actual listing cards as
  // well, rather than mistaking the results-page heading for a single place.
  for (const card of document.querySelectorAll('[role="feed"] [role="article"]')) {
    const link = card.querySelector('a.hfpxzc') || card.querySelector('a[href*="/maps/place/"]');
    if (!link?.href) continue;
    const name = (link.getAttribute("aria-label") || link.textContent || "").trim();
    if (!name || /^(results|google maps)$/i.test(name)) continue;
    const identity = getListingIdentity(link.href);
    // Use the leaf category/address row, not an outer metadata wrapper
    // that also contains opening times and the telephone number.
    const line = [...card.querySelectorAll(".W4Efsd")]
      .filter(node => !node.querySelector(".W4Efsd, .AJB7ye, .MW4etd, .UY7F9"))
      .map(node => node.innerText.trim())
      .find(text => text && !/^(?:No reviews$|(?:Open|Closed|Closes soon)\s*(?:·|$)|Opens\s+\d|Temporarily closed|Permanently closed)/i.test(text)) || "";
    const parts = line.split("·").map(text => text.trim())
      .filter(text => text && !/^[\uE000-\uF8FF\s]+$/.test(text));
    const address = parts.length > 1 ? parts[parts.length - 1] : "";
    const row = {
      name,
      phone: card.querySelector(".UsdlK")?.textContent.trim() || "",
      website: card.querySelector('a[aria-label^="Visit "][aria-label$="website"]')?.href || "",
      address,
      email: "",
      ...identity,
      category: parts[0] || "",
      reviewCount: (card.querySelector(".UY7F9")?.textContent || "").replace(/[^0-9]/g, ""),
      averageRating: card.querySelector(".MW4etd")?.textContent.trim() || ""
    };
    storeLead(row);
  }
  updateLeadCounter();
}

async function tryExtractSinglePlace() {
  // Only a place-details heading is a business. A generic h1 can be
  // the search title ("Results") or the extension's own lead counter.
  const name =
    getTextBySelector("h1.DUwDvf") ||
    getTextBySelector("h1.fontHeadlineLarge");

  if (!name || /^(results|google maps|leads:\s*\d+)$/i.test(name)) return null;

  const website =
    getAttrBySelector('a[data-item-id="authority"]', "href") ||
    getAttrBySelector('a[aria-label*="Website"]', "href");

  const phone =
    getTextBySelector('button[data-item-id^="phone:tel:"] .Io6YTe') ||
    getTextBySelector('button[aria-label^="Phone:"] .Io6YTe');

  const address =
    getTextBySelector('button[data-item-id^="address"] .Io6YTe') ||
    getTextBySelector('button[aria-label^="Address:"] .Io6YTe');

  const category =
    getTextBySelector('button[jsaction*="pane.rating.category"]') ||
    getTextBySelector(".DkEaL");

  const reviewCount = getTextBySelector(".UY7F9");
  const averageRating = getTextBySelector(".MW4etd");
  const placeID = getPlaceIdFromUrl();
  const { latitude, longitude } = getLatLngFromUrl();

  const cID = getListingIdentity(window.location.href).cID;
  const uniqueKey = placeID || cID || `${name}|${address}`;
  if (!uniqueKey || leads_lnglat.has(uniqueKey)) return null;
  leads_lnglat.add(uniqueKey);

  const row = {
    name,
    phone,
    website,
    address,
    email: "",
    placeID,
    cID,
    category,
    reviewCount,
    averageRating,
    latitude,
    longitude
  };

  if (website && collect_email) {
    try {
      const enriched = await chrome.runtime.sendMessage({
        action: "email",
        data: { website, name, deep_search: true }
      });
      if (enriched) {
        for (const key in enriched) {
          row[key] = Array.isArray(enriched[key]) ? enriched[key].join() : enriched[key];
        }
      }
    } catch (err) {
      console.warn("collect email error(single item):", err);
    }
  }

  leads.push(row);
  updateLeadCounter();
  return row;
}

function getCollectionStopReason(feed) {
  const text = document.body?.innerText || "";
  const visibleChallenge = [...document.querySelectorAll('#captcha-form, form[action*="/sorry/"], iframe[src*="recaptcha"]')]
    .some(node => node.getClientRects().length && node.offsetHeight > 0);
  if (/\/sorry(?:\/|\?)/i.test(location.href) || visibleChallenge ||
      /our systems have detected unusual traffic|verify (?:that )?you(?:.re| are) (?:a )?human|i.m not a robot/i.test(text)) return "verification";
  if (/^https?:\/\/consent\.google\./i.test(location.href) || /before you continue to google/i.test(text)) return "consent";
  if (!feed) return "";
  if (/you['’]ve reached the end of the list\.?/i.test(feed.innerText)) return "end_of_list";
  return "";
}

function getResultsScrollContainer(feed) {
  const candidates = [feed, ...feed.children];
  let ancestor = feed.parentElement;
  for (let depth = 0; ancestor && ancestor !== document.body && depth < 4; depth++) {
    candidates.push(ancestor);
    ancestor = ancestor.parentElement;
  }
  return candidates.find(node => {
    const overflow = getComputedStyle(node).overflowY;
    return /auto|scroll/.test(overflow) && node.scrollHeight > node.clientHeight + 2;
  }) || feed;
}

(function insertPanel() {
  const container = document.createElement("div");
  container.className = "extension_gms_page";

  const leadsInfo = document.createElement("h1");
  leadsInfo.id = "extension_gms_leads_info";
  leadsInfo.innerHTML = "Leads: 0";

  const startBtn = document.createElement("button");
  startBtn.className = "extension_gms_button";
  startBtn.innerText = "Start Auto Extract";
  startBtn.id = "extension_gms_start_btn";
  startBtn.dataset.collectionState = "idle";
  startBtn.addEventListener("click", async (event) => {
    const target = event.target;
    if (auto_extract_flag) {
      target.dataset.stopReason = "manual_stop";
      target.dataset.collectionState = "stopping";
      auto_extract_flag = false;
      return;
    }

    target.innerText = "Stop Auto Extract";
    target.classList.add("active-stop");
    target.dataset.runId = `${Date.now()}-${Math.random()}`;
    target.dataset.collectionState = "running";
    target.dataset.stopReason = "running";
    target.dataset.limitedView = "false";
    target.dataset.endReached = "false";
    auto_extract_flag = true;

    try {
      let previousHeight = -1;
      let idleRounds = 0;
      while (auto_extract_flag) {
        let feed = document.querySelector('[role="feed"]');
        collectVisibleSearchResults();
        if (feed && /limited view of google maps/i.test(feed.innerText)) {
          target.dataset.limitedView = "true";
          leadsInfo.title = "Google Maps reports limited view; continuing to scroll available results.";
        }
        const reason = getCollectionStopReason(feed);
        if (reason === "end_of_list") {
          // Let the final batch settle, then confirm the actual end message.
          await wait(750);
          feed = document.querySelector('[role="feed"]');
          collectVisibleSearchResults();
          if (getCollectionStopReason(feed) === "end_of_list") {
            target.dataset.stopReason = "end_of_list";
            target.dataset.endReached = "true";
            break;
          }
          continue;
        }
        if (reason) {
          target.dataset.stopReason = reason;
          break;
        }
        if (!feed) {
          const row = await tryExtractSinglePlace();
          if (row) {
            target.dataset.stopReason = "single_place";
            break;
          }
          if (/no results found|google maps can.t find/i.test(document.body.innerText)) {
            target.dataset.stopReason = "no_results";
            break;
          }
          // A not-yet-rendered/replaced feed is not a finished list.
          await wait(1000);
          continue;
        }
        const scroller = getResultsScrollContainer(feed);
        // Collect each viewport rather than jumping past virtualized cards.
        const step = Math.max(200, Math.floor(scroller.clientHeight * 0.8));
        scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + step);
        await wait(1000 * (Math.floor(Math.random() * 3) + 1));
        collectVisibleSearchResults();
        const atBottom = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 3;
        idleRounds = atBottom && previousHeight === scroller.scrollHeight ? idleRounds + 1 : 0;
        previousHeight = scroller.scrollHeight;
        // A slow/stalled batch is not completion. Nudge and keep waiting.
        if (idleRounds >= 3 && auto_extract_flag && !getCollectionStopReason(document.querySelector('[role="feed"]'))) {
          scroller.scrollTop = Math.max(0, scroller.scrollTop - step);
          await wait(300);
          collectVisibleSearchResults();
          scroller.scrollTop = Math.min(scroller.scrollHeight, scroller.scrollTop + step);
        }
      }
    } catch (error) {
      target.dataset.stopReason = "error";
      console.warn("Extension collection interrupted:", error);
    } finally {
      target.dataset.collectionState = "finishing";
      // Publish completion only after the extension has collected the last batch.
      try {
        await wait(0);
        collectVisibleSearchResults();
      } catch (error) {
        target.dataset.stopReason = "error";
        console.warn("Extension final collection interrupted:", error);
      } finally {
        if (target.dataset.stopReason === "running") target.dataset.stopReason = "manual_stop";
        target.innerText = "Start Auto Extract";
        target.classList.remove("active-stop");
        auto_extract_flag = false;
        target.dataset.collectionState = "finished";
      }
    }
  });

  const downloadBtn = document.createElement("button");
  downloadBtn.className = "extension_gms_button extension_gms_btn_export";
  downloadBtn.innerText = "Export Leads (0)";
  downloadBtn.id = "extension_gms_download_btn";
  downloadBtn.addEventListener("click", async () => {
    collectVisibleSearchResults();
    if (!leads.length) {
      await tryExtractSinglePlace();
    }
    chrome.runtime.sendMessage({ action: "openPage", data: leads });
    console.log("leads:", leads);
  });

  const clearBtn = document.createElement("button");
  clearBtn.className = "extension_gms_button extension_gms_btn_clear";
  clearBtn.innerText = "Clear";
  clearBtn.id = "extension_gms_clear_btn";
  clearBtn.addEventListener("click", async () => {
    leads = [];
    leads_lnglat.clear();
    updateLeadCounter();
  });

  container.appendChild(leadsInfo);
  container.appendChild(startBtn);
  container.appendChild(downloadBtn);
  container.appendChild(clearBtn);

  setInterval(() => {
    const host = document.getElementsByClassName("w6VYqd")[0];
    if (host && !host.contains(container)) {
      host.appendChild(container);
    }
  }, 2000);
})();

function decode_cf_email(a){s="";r=parseInt(a.substr(0,2),16);for(j=2;a.length-j;j+=2)c=parseInt(a.substr(j,2),16)^r,s+=String.fromCharCode(c);return s}
function get_domain(a){const e=new Set("ac ad ae af ag ai al am an ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et eu fi fj fk fm fo fr ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug uk us uy uz va vc ve vg vi vn vu wf ws xk ye yt za zm zw".split(" "));a=(new URL(a)).host.toLowerCase().split(".");return e.has(a[a.length-1])?a[a.length-3]:a[a.length-2]}
function normalize_social_link(a){try{a.startsWith("//")&&(a="https:"+a);a.startsWith("http")||(a="https://"+a);const e=new Set("/reel /about /tr /privacy /download /pg /settings /vp /profiles".split(" "));let f=new URL(a);if("http:"===f.protocol||""===f.protocol)f.protocol="https:";"instagram.com"===f.host&&(f.host="www.instagram.com");"facebook.com"===f.host&&(f.host="www.facebook.com");"yelp.com"===f.host&&(f.host="www.yelp.com");"www.twitter.com"===f.host&&(f.host="twitter.com");"/"===f.pathname[f.pathname.length-1]&&(f.pathname=f.pathname.slice(0,-1));return e.has(f.pathname)?"":f.toString()}catch(e){console.warn("normalize_social_link error: ",a,e)}return""}
async function extractemail(a,e,f){try{a.startsWith("//")&&(a="https:"+a);a.startsWith("http")||(a="https://"+a);const t=await chrome.runtime.sendMessage({action:"access",data:{url:a}});if(10>t.length)console.warn("visit error: ",a);else{var g=t.normalize("NFKC");e={instagram:/(((http|https):\/\/)?((www\.)?(?:instagram.com|instagr.am)\/([A-Za-z0-9_.]{2,30})))/ig,facebook:/(?:https?:)?\/\/(?:www\.)?(?:facebook|fb)\.com\/((?![A-z]+\.php)(?!marketplace|gaming|watch|me|messages|help|search|groups)[A-z0-9_\-\.]+)\/?/ig,youtube:/(?:https?:)?\/\/(?:[A-z]+\.)?youtube\.com\/(channel\/([A-z0-9-_]+)|user\/([A-z0-9]+))\/?/ig,linkedin:/(?:https?:)?\/\/(?:[\w]+\.)?linkedin\.com\/((company|school)\/[A-z0-9-\u00c0-\u00ff\.]+|in\/[\w\-_\u00c0-\u00ff%]+)\/?/ig,twitter:/(?:(?:http|https):\/\/)?(?:www.)?(?:twitter.com)\/(?!(oauth|account|tos|privacy|signup|home|hashtag|search|login|widgets|i|settings|start|share|intent|oct)(['"\?\.\/]|$))([A-Za-z0-9_]{1,15})/igm,email:/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,yelp:/https?:\/\/(www\.)?yelp\.com\/biz\/[a-zA-Z0-9_-]+/ig};var k=new Set,d={};for(const b in e){d[b]=new Set;var h=g.match(e[b]);h&&h.forEach(l=>{l&&("email"===b?d[b].add(l):(l=normalize_social_link(l))&&d[b].add(l))})}var p=new URL(a);try{var q=(new DOMParser).parseFromString(g,"text/html").querySelector(".__cf_email__");if(q){const b=q.getAttribute("data-cfemail");b&&d.email.add(decode_cf_email(b))}}catch(b){console.warn("DOMParser parsed error: ",a,b)}q=/<a\s+(?:[^>]*?\s+)?href=(["'])(.*?)\1/gi;h=[];for(var n;n=q.exec(g);)h.push((new URL(n[2],p)).toString());g="/contact /contact-us /contact-me /about /about-me /about-us /team /our-team /meet-the-team /support /customer-service /feedback /help /sales privacy return location policy faq".split(" ");for(p=0;p<h.length;p++){var m=h[p];for(n=0;n<g.length;++n)if(m.includes(g[n])){k.add(m);break}}for(m=0;m<h.length;m++)try{const b=h[m];if(!b)continue;const l=(new URL(b)).host.toLowerCase();for(const u in e)if(l.includes(u)){if(0>=d[u].size){const v=normalize_social_link(b);v&&d[u].add(v)}break}}catch(b){console.warn(`error: ${h[m]}`,b)}if(f&&0<k.size){const b=[...k].map(async l=>await extractemail(l,"",!1));(await Promise.all(b)).map(l=>{if(l)for(const u in l)l[u].forEach(v=>{d[u].add(v)})})}console.log("Email for : ",a,d,[...k].join());var w=new Set,x=new Set,y=".png .jpg .jpeg .gif .webp wixpress.com sentry.io noreply abuse no-reply subscribe mailer-daemon domain.com email.com yourname wix.com".split(" "),z=get_domain(a);d.email.forEach(b=>{b=b.replace("u003e","").toLowerCase();for(let l=0;l<y.length;++l)if(b.includes(y[l]))return;w.add(b);z&&b.includes(z)&&x.add(b)});d.email=0<x.size?x:w;return d}}catch(t){console.log(`visit url error: ${a}`,t)}}
async function findemails(a){const e=new URL(a);var f=[];"/contact /contact-us /contact-me /about /about-me /about-us /team /our-team /meet-the-team /support /customer-service /feedback /help /sales".split(" ").map(g=>{f.push((new URL(g,e)).toString())});a=f.map(async g=>await extractemail(g,""));return(await Promise.all(a)).filter(g=>g&&""!==g.trim()).join(",")}
window.addEventListener("message", async event => {
  if (event.source !== window || event.data?.type !== "search" || !event.data.data) return;
  try {
    const envelope = JSON.parse(event.data.data.replace('/*""*/', ""));
    const results = JSON.parse(envelope.d.slice(5));
    const feed = results[64];
    if (!Array.isArray(feed)) return;
    const rows = [];
    for (const entry of feed) {
      try {
        const item = entry?.[entry.length - 1];
        if (!Array.isArray(item) || !item[11]) continue;
        const hours = {};
        for (const day of item[203]?.[0] || []) {
          if (day?.[0] && day[1] != null) {
            hours[`${day[1]}_${day[0]}`] = (day[3] || []).map(period => period[0]).join(", ");
          }
        }
        rows.push({
          name: item[11], phone: item[178]?.[0]?.[0] || "",
          website: item[7]?.[0] || "", address: (item[2] || []).join(","), email: "",
          placeID: item[78] || "", cID: item[37]?.[0]?.[0]?.[29]?.[1] || "",
          category: (item[13] || []).join(";"),
          reviewCount: item[4]?.[8] ?? "", averageRating: item[4]?.[7] ?? "",
          latitude: item[9]?.[2] ?? "", longitude: item[9]?.[3] ?? "", ...hours
        });
      } catch (error) {
        console.warn("Unsupported Maps listing; continuing with other results:", error);
      }
    }
    for (const row of rows) {
      if (row.website && collect_email) {
        try {
          const extra = await chrome.runtime.sendMessage({action: "email", data: {
            website: row.website, name: row.name, deep_search: true
          }});
          for (const [key, value] of Object.entries(extra || {})) {
            row[key] = Array.isArray(value) ? value.join() : value;
          }
        } catch (error) { console.warn("collect email error:", error); }
      }
      // Full response details enrich a DOM row instead of being discarded as
      // a duplicate. Empty network fields never erase known card values.
      storeLead(row, true);
    }
    updateLeadCounter();
  } catch (error) {
    // A changed/unsupported response must not abort visible-card extraction.
    console.warn("Unsupported Maps search response; using visible listings:", error);
  }
});
