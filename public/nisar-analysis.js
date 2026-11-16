"use strict";

(() => {
  const panel = document.getElementById("nisar-analysis");
  const get = name => panel.querySelector("#nisar-" + name);
  const site = get("site");
  const year = get("year");
  const pair = get("pair");
  const status = get("status");
  const run = get("run");
  const map = get("map");
  const ctx = map.getContext("2d");
  const list = get("list");
  const threshold = get("threshold");
  const overview = name => document.getElementById("overview-" + name);
  const area = new Intl.NumberFormat("en-US", {maximumFractionDigits: 3});
  const reviewKey = "cryotrace-nisar-review";
  const explorer = name => document.getElementById("explorer-" + name);
  let opticalSearch;
  const opticalCenters = new Map();
  let comparison;
  let visible = [];
  let selected;
  let saved = readReview();
  const jobLabels = {
    queued: "Waiting for the analysis worker…",
    discovering: "Checking NASA for a compatible pair…",
    retrieving: "Retrieving radar data…",
    analyzing: "Reading glacier radar subsets and measuring change…",
  };

  function readReview() {
    try {
      const records = JSON.parse(localStorage.getItem(reviewKey) || "[]");
      return Array.isArray(records) ? records : [];
    } catch {
      return [];
    }
  }

  async function json(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => null);
    if (!response.ok || !data) throw new Error(data?.error || "The CryoTrace server could not load this request.");
    return data;
  }

  function image(url, errorMessage = "A radar image could not be loaded.") {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error(errorMessage));
      img.src = url;
    });
  }

  function element(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }

  function projectGeometry(bounds) {
    const [west, south, east, north] = bounds;
    // GCOV overlays are rendered to Web Mercator before publication.
    const mercator = latitude => Math.asinh(Math.tan(latitude * Math.PI / 180));
    const top = mercator(north);
    const height = top - mercator(south);
    const point = ([lon, lat]) => [
      (lon - west) / (east - west) * map.width,
      (top - mercator(lat)) / height * map.height,
    ];
    return geometry => {
      const path = new Path2D();
      if (geometry.type === "Point") {
        path.arc(...point(geometry.coordinates), 3, 0, Math.PI * 2);
      } else {
        const polygons = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;
        for (const polygon of polygons) {
          for (const ring of polygon) {
            path.moveTo(...point(ring[0]));
            for (const coordinate of ring.slice(1)) path.lineTo(...point(coordinate));
            path.closePath();
          }
        }
      }
      return path;
    };
  }

  async function loadBundle(url) {
    const bundle = await json(url);
    const asset = relative => new URL(relative, url).href;
    const layers = bundle.referenceLayers;
    const [earlier, later, patches, boundary, points] = await Promise.all([
      image(asset(bundle.before.imageUrl)), image(asset(bundle.after.imageUrl)), json(asset(bundle.patchesUrl)),
      layers ? json(asset(layers.boundaryUrl)) : null,
      layers ? json(asset(layers.pointsUrl)) : null,
    ]);
    map.width = earlier.naturalWidth;
    map.height = earlier.naturalHeight;
    const path = projectGeometry(bundle.bounds);
    comparison = {
      bundle, earlier, later,
      patches: patches.features.sort((a, b) => a.properties.rank - b.properties.rank)
        .map(feature => ({...feature, path: path(feature.geometry)})),
      boundary: boundary?.features.map(feature => path(feature.geometry)) || [],
      points: points?.features.map(feature => path(feature.geometry)) || [],
    };
    updateExplorerSync();
    selected = undefined;
    threshold.max = Math.max(1, Math.ceil(Math.max(...comparison.patches.map(p => Math.abs(p.properties.medianDeltaDb)), 0)));
    threshold.value = 0;
    get("swipe").value = 50;
    get("comparison").textContent = bundle.before.date + " → " + bundle.after.date;
    get("reference-control").hidden = !layers;
    get("source").href = "https://doi.org/" + bundle.sources.collectionDoi;
    get("source").textContent = "NISAR " + bundle.sources.maturity + " source ↗";
    get("result").hidden = false;
    renderList();
    renderEvidence();
  }

  function updateExplorerSync() {
    const active = comparison?.bundle.siteId === site.value;
    const button = explorer("sync");
    button.disabled = !active;
    button.title = active ? "" : "Run a comparison in the NISAR map first.";
    explorer("map-link").hidden = !active || explorer("site").value !== site.value;
  }

  function setExplorerSwipe(value) {
    explorer("landsat-wipe").value = value;
    explorer("landsat-scene").style.setProperty("--split", value + "%");
  }

  async function renderExplorer(targets, swipe = 50) {
    opticalSearch?.abort();
    const request = new AbortController();
    opticalSearch = request;
    const picker = explorer("site");
    const glacier = picker.selectedOptions[0]?.textContent;
    const archive = picker.value === "columbia" && !targets;
    explorer("optical-guide").hidden = true;
    const glacierInfo = {id: picker.value, name: glacier, coordinates: opticalCenters.get(picker.value)};
    document.dispatchEvent(new CustomEvent("cryotrace-optical", {detail: {archive}}));
    explorer("optical").hidden = explorer("optical-controls").hidden = !archive;
    explorer("landsat").hidden = true;
    explorer("empty").hidden = archive;
    explorer("seasonal").hidden = !targets && picker.value !== "columbia";
    if (archive) explorer("seasonal").hidden = true;
    document.getElementById("obs-title").textContent = glacier;
    explorer("description").textContent = "Move the divider to compare two dates of Landsat imagery.";
    explorer("source").textContent = archive ? "Landsat optical archive" : "False-color Landsat";
    explorer("status").textContent = targets ? "NISAR dates " + targets.before + " → " + targets.after : "";
    updateExplorerSync();
    if (archive) return;

    explorer("message").textContent = targets ? "Finding Landsat scenes near the NISAR dates…" : "Finding seasonal Landsat scenes…";
    const query = targets ? "?" + new URLSearchParams(targets) : "";
    try {
      const scenes = await json("/api/sites/" + encodeURIComponent(picker.value) + "/optical" + query, {signal: request.signal});
      const [earlier, later] = await Promise.all([
        image(scenes.before.imageUrl, "The Landsat image service could not render this pair. Try the seasonal view."),
        image(scenes.after.imageUrl, "The Landsat image service could not render this pair. Try the seasonal view."),
      ]);
      if (request.signal.aborted) return;
      explorer("landsat-scene").style.aspectRatio = earlier.naturalWidth + " / " + earlier.naturalHeight;
      const credit = explorer("landsat-credit");
      credit.replaceChildren();
      for (const [side, rendered] of [["before", earlier], ["after", later]]) {
        const acquired = scenes[side];
        const img = explorer("landsat-" + side);
        img.src = rendered.src;
        img.alt = glacier + " Landsat scene from " + acquired.date;
        explorer("landsat-" + side + "-date").textContent = acquired.date;
        const note = element("p", (side === "before" ? "Earlier" : "Later") + " · ");
        const link = element("a", acquired.date + " · " + acquired.platform + " ↗");
        link.href = acquired.sourceUrl;
        link.target = "_blank";
        link.rel = "noopener";
        note.append(link);
        if (targets) note.append(" · " + (acquired.offsetDays === 0 ? "same date as NISAR"
          : Math.abs(acquired.offsetDays) + " days " + (acquired.offsetDays < 0 ? "before" : "after") + " NISAR"));
        credit.append(note);
      }
      setExplorerSwipe(swipe);
      explorer("empty").hidden = true;
      explorer("landsat").hidden = false;
      explorer("optical-guide").hidden = false;
      document.dispatchEvent(new CustomEvent("cryotrace-optical", {
        detail: {archive: false, glacier: glacierInfo, scenes: {before: scenes.before, after: scenes.after}},
      }));
    } catch (error) {
      if (request.signal.aborted) return;
      explorer("message").textContent = error.message;
      explorer("seasonal").hidden = false;
    }
  }

  explorer("site").addEventListener("change", () => renderExplorer());
  explorer("seasonal").addEventListener("click", () => renderExplorer());
  explorer("sync").addEventListener("click", () => {
    if (comparison?.bundle.siteId !== site.value) return;
    explorer("site").value = comparison.bundle.siteId;
    renderExplorer({before: comparison.bundle.before.date, after: comparison.bundle.after.date}, get("swipe").value);
  });
  explorer("landsat-wipe").addEventListener("input", event => setExplorerSwipe(event.target.value));

  function draw() {
    const {earlier, later, boundary, points} = comparison;
    const split = map.width * Number(get("swipe").value) / 100;
    ctx.clearRect(0, 0, map.width, map.height);
    // Draw the dates in non-overlapping slices so transparent margins match.
    ctx.save();
    ctx.beginPath();
    ctx.rect(split, 0, map.width - split, map.height);
    ctx.clip();
    ctx.drawImage(later, 0, 0);
    ctx.restore();
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, split, map.height);
    ctx.clip();
    ctx.drawImage(earlier, 0, 0);
    ctx.restore();
    if (get("reference").checked) {
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 5]);
      ctx.strokeStyle = "#edce7b";
      for (const path of boundary) ctx.stroke(path);
      ctx.setLineDash([]);
      ctx.fillStyle = "#edce7b";
      for (const path of points) ctx.fill(path);
    }
    for (const patch of visible) {
      const gain = patch.properties.medianDeltaDb > 0;
      ctx.fillStyle = gain ? "#77f3ec42" : "#ffb27e42";
      ctx.strokeStyle = gain ? "#77f3ec" : "#ffb27e";
      ctx.lineWidth = 2;
      ctx.fill(patch.path, "evenodd");
      ctx.stroke(patch.path);
    }
    if (selected) {
      ctx.lineWidth = 5;
      ctx.strokeStyle = "#fff";
      ctx.stroke(selected.path);
    }
    if (split > 0 && split < map.width) {
      ctx.fillStyle = "#fff";
      ctx.fillRect(split - 1, 0, 2, map.height);
    }
  }

  function renderList() {
    const floor = Number(threshold.value);
    visible = comparison.patches.filter(patch => Math.abs(patch.properties.medianDeltaDb) >= floor);
    if (selected && !visible.includes(selected)) {
      selected = undefined;
      renderEvidence();
    }
    get("db").textContent = floor.toFixed(1) + " dB";
    get("count").textContent = visible.length + " of " + comparison.patches.length + " candidates shown";
    list.replaceChildren(...visible.map(patch => {
      const row = element("li", "");
      const button = element("button", "");
      const {rank, medianDeltaDb} = patch.properties;
      button.type = "button";
      button.dataset.patch = patch.id;
      button.setAttribute("aria-pressed", String(patch === selected));
      button.append(element("span", "Patch " + rank), element("span", formatDb(medianDeltaDb)));
      row.append(button);
      return row;
    }));
    draw();
    renderOverview();
  }

  function formatDb(value) {
    return (value > 0 ? "+" : "") + value.toFixed(2) + " dB";
  }

  function selectPatch(patch) {
    selected = patch;
    for (const button of list.querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(button.dataset.patch === patch.id));
    }
    renderEvidence();
    draw();
    renderOverview();
  }

  function renderOverview() {
    const current = comparison?.bundle.siteId === site.value ? comparison : null;
    const glacier = site.selectedOptions[0]?.textContent;
    const dates = overview("pair");
    const metrics = overview("measurements");
    const findings = overview("findings");
    const chosen = pair.value.replace("/", " → ");
    overview("glacier").textContent = glacier || "Glacier comparison";
    dates.hidden = !current && !chosen;
    dates.textContent = current
      ? current.bundle.before.date + " → " + current.bundle.after.date
      : chosen;
    const differentPair = current && chosen && chosen !== dates.textContent;
    overview("status").textContent = differentPair
      ? "Selected " + chosen + ". The map still shows the dates above. " + status.textContent
      : status.textContent || (current ? "" : "Choose an available comparison and run it to see findings.");
    metrics.hidden = !current;
    metrics.replaceChildren();
    findings.replaceChildren();
    if (!current) {
      findings.append(element("p", "Radar measurements will appear here when this glacier’s comparison is ready."));
      return;
    }

    const increases = visible.filter(patch => patch.properties.medianDeltaDb > 0).length;
    const totalArea = visible.reduce((sum, patch) => sum + patch.properties.areaM2, 0);
    metrics.append(
      element("li", visible.length + " of " + current.patches.length +
        " candidates shown at ≥ " + Number(threshold.value).toFixed(1) + " dB absolute median change"),
      element("li", increases + " increases · " + (visible.length - increases) + " decreases in radar backscatter"),
      element("li", area.format(totalArea / 1e6) + " km² across visible patches"),
    );
    const patch = selected || visible[0];
    if (patch) {
      const {rank, explanation, reference} = patch.properties;
      findings.append(element("p", (selected ? "Selected patch " : "Highest-ranked visible patch ") + rank + ". " + explanation));
      if (reference?.nearbyPoint) {
        const point = reference.nearbyPoint;
        const labels = current.bundle.sources.reference.interpretationLabels;
        findings.append(element("p", "A preliminary USGS inventory point lies " + point.distanceM +
          " m from the patch. USGS interpretation: " + labels[point.interpretationCode] + "."));
      }
    } else {
      findings.append(element("p", current.patches.length ? "Lower the change threshold to see more candidates."
        : "The detector found no patches at the detection settings used for this comparison."));
    }
    findings.append(element("p", "Backscatter can change with snow, surface moisture, or ice motion. The comparison measures radar change and leaves the cause open."));
  }

  function isSaved() {
    const {bundle} = comparison;
    return saved.some(item => item.siteId === bundle.siteId && item.before === bundle.before.date
      && item.after === bundle.after.date && item.patchId === selected.id);
  }

  function renderEvidence() {
    const evidence = get("evidence");
    const save = get("save");
    save.hidden = !selected;
    if (!selected) {
      evidence.replaceChildren(element("p", "Select a patch to see its measurements."));
      return;
    }
    const {rank, medianDeltaDb, areaM2, explanation, reference} = selected.properties;
    evidence.replaceChildren(
      element("h3", "Patch " + rank),
      element("p", formatDb(medianDeltaDb) + " · " + (areaM2 / 1e6).toFixed(3) + " km²"),
      element("p", explanation),
    );
    if (reference) {
      const nearby = reference.nearbyPoint;
      const labels = comparison.bundle.sources.reference.interpretationLabels;
      const coverage = {inside: "Inside", partial: "Partly inside", outside: "Outside"};
      evidence.append(element("p", coverage[reference.surveyCoverage] + " the preliminary USGS survey area."));
      evidence.append(element("p", nearby
        ? (labels[nearby.interpretationCode] || "USGS mapped point") + " · " + nearby.distanceM + " m from this patch."
        : "No USGS inventory point within " + reference.searchRadiusM + " m."));
    }
    save.disabled = isSaved();
    save.textContent = save.disabled ? "Patch saved" : "Save patch";
  }

  function showReviewCount() {
    get("export").disabled = !saved.length;
    get("review").textContent = saved.length ? saved.length + " saved for review" : "";
  }

  list.addEventListener("click", event => {
    const button = event.target.closest("button[data-patch]");
    if (button) selectPatch(visible.find(patch => patch.id === button.dataset.patch));
  });
  map.addEventListener("click", event => {
    const box = map.getBoundingClientRect();
    const x = (event.clientX - box.left) / box.width * map.width;
    const y = (event.clientY - box.top) / box.height * map.height;
    const patch = visible.find(item => ctx.isPointInPath(item.path, x, y, "evenodd"));
    if (patch) selectPatch(patch);
  });
  threshold.addEventListener("input", renderList);
  get("swipe").addEventListener("input", draw);
  get("reference").addEventListener("change", draw);
  get("save").addEventListener("click", () => {
    if (!selected || isSaved()) return;
    const {bundle} = comparison;
    const {areaM2, medianDeltaDb} = selected.properties;
    const next = [...saved, {
      siteId: bundle.siteId, before: bundle.before.date, after: bundle.after.date,
      patchId: selected.id, areaM2, medianDeltaDb,
    }];
    try {
      localStorage.setItem(reviewKey, JSON.stringify(next));
      saved = next;
      showReviewCount();
      renderEvidence();
    } catch {
      get("review").textContent = "Browser storage could not save the patch.";
    }
  });
  get("export").addEventListener("click", () => {
    const fields = ["siteId", "before", "after", "patchId", "areaM2", "medianDeltaDb"];
    const records = [fields, ...saved.map(record => fields.map(field => record[field]))];
    const csv = records.map(record => record.map(value => '"' + String(value).replaceAll('"', '""') + '"').join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], {type: "text/csv"}));
    const link = document.createElement("a");
    link.href = url;
    link.download = "cryotrace-review.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  function setPairs(pairs, preferred) {
    const options = pairs.map(({before, after}) => {
      const option = element("option", before + " → " + after);
      option.value = before + "/" + after;
      return option;
    });
    pair.replaceChildren(...options);
    if (options.some(option => option.value === preferred)) pair.value = preferred;
    pair.disabled = run.disabled = options.length === 0;
    renderOverview();
  }

  async function findPairs(preferred) {
    const siteId = site.value;
    const selectedYear = year.value;
    const dateStatus = get("date-status");
    const retry = get("find");
    const request = new AbortController();
    dateSearch?.abort();
    dateSearch = request;
    retry.hidden = true;
    setPairs([]);
    dateStatus.textContent = "Looking up compatible passes in " + selectedYear + "…";
    try {
      const url = "/api/sites/" + encodeURIComponent(siteId) + "/dates?start=" + selectedYear + "-01-01&end=" + selectedYear + "-12-31";
      const data = await json(url, {signal: request.signal});
      if (request.signal.aborted) return;
      if (!Array.isArray(data.pairs)) throw new Error("Restart the CryoTrace server to load the updated date picker.");
      setPairs(data.pairs, preferred);
      if (!data.catalogAvailable) {
        retry.hidden = false;
        dateStatus.textContent = data.pairs.length ? "Showing the saved comparison while NASA’s catalog is unavailable."
          : "NASA’s catalog is unavailable. Try again.";
      } else {
        dateStatus.textContent = data.pairs.length ? "" : "No compatible comparisons found for this year.";
      }
    } catch (error) {
      if (request.signal.aborted) return;
      dateStatus.textContent = error.message;
      retry.hidden = false;
    }
  }

  let dateSearch;
  const latestYear = new Date().getFullYear();
  for (let value = latestYear; value >= 2025; value--) year.append(element("option", String(value)));
  site.addEventListener("change", () => {
    const current = site.value;
    updateExplorerSync();
    get("result").hidden = comparison?.bundle.siteId !== current;
    status.textContent = "";
    year.value = current === "hubbard" ? "2025" : String(latestYear);
    findPairs(current === "hubbard" ? "2025-11-26/2025-12-08" : undefined);
  });
  year.addEventListener("change", () => findPairs());
  pair.addEventListener("change", renderOverview);
  new MutationObserver(renderOverview).observe(status, {childList: true, subtree: true, characterData: true});
  get("find").addEventListener("click", () => findPairs());

  get("form").addEventListener("submit", async event => {
    event.preventDefault();
    if (run.disabled || !pair.value) return;
    const [before, after] = pair.value.split("/");
    const controls = [run, site, year, pair, get("find")];
    const request = {siteId: site.value, before, after};
    controls.forEach(control => { control.disabled = true; });
    status.textContent = "Submitting the comparison…";
    try {
      let job = await json("/api/jobs", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(request),
      });
      while (jobLabels[job.status]) {
        status.textContent = jobLabels[job.status];
        await new Promise(resolve => setTimeout(resolve, 1500));
        job = await json(job.statusUrl);
      }
      if (job.status !== "succeeded") throw new Error(job.error || "The comparison could not finish.");
      await loadBundle(new URL(job.bundleUrl, location.href));
      status.textContent = "Comparison complete.";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      controls.forEach(control => { control.disabled = false; });
    }
  });

  async function initialize() {
    site.disabled = year.disabled = true;
    try {
      const catalogUrl = new URL("/data/sites.json", location.href);
      const catalog = await json(catalogUrl);
      site.replaceChildren(...catalog.sites.filter(item => item.status !== "fixture").map(item => {
        const option = element("option", item.name);
        option.value = item.id;
        return option;
      }));
      for (const item of catalog.sites) opticalCenters.set(item.id, {lat: item.center[1], lon: item.center[0]});
      explorer("site").replaceChildren(...Array.from(site.options, option => option.cloneNode(true)));
      explorer("site").value = "columbia";
      renderExplorer();
      const hubbard = catalog.sites.find(item => item.id === "hubbard");
      await loadBundle(new URL(hubbard.bundleUrl, catalogUrl));
      year.value = comparison.bundle.after.date.slice(0, 4);
      setPairs([{before: comparison.bundle.before.date, after: comparison.bundle.after.date}]);
      findPairs(comparison.bundle.before.date + "/" + comparison.bundle.after.date);
      status.textContent = "Showing the November–December 2025 Hubbard comparison.";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      if (site.options.length) {
        site.disabled = year.disabled = false;
      }
      showReviewCount();
    }
  }

  initialize();
})();
