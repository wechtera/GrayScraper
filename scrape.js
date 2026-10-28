const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
const fs = require('fs');

chromium.use(stealth);

(async () => {
  if (!fs.existsSync('cookies.json')) {
    console.error('cookies.json not found.');
    return;
  }

  const rawCookies = JSON.parse(fs.readFileSync('cookies.json', 'utf8'));
  const cleanCookies = (Array.isArray(rawCookies) ? rawCookies : []).map(c => ({
    name: c.name,
    value: c.value,
    domain: c.domain || '.myminifactory.com',
    path: c.path || '/',
    secure: Boolean(c.secure),
    httpOnly: Boolean(c.httpOnly),
    expires: c.expirationDate ? Number(c.expirationDate) : undefined,
    sameSite: ['Strict', 'Lax', 'None'].includes(c.sameSite) ? c.sameSite : 'None'
  }));

  // Launch installed Chrome if available; fallback to standard Chromium
  let browser;
  try {
    browser = await chromium.launch({
      channel: 'chrome',
      headless: false,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars'
      ]
    });
  } catch (err) {
    console.log('[Browser] Chrome channel launch failed, falling back to default Chromium...');
    browser = await chromium.launch({
      headless: false,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
    });
  }

  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
  });

  if (cleanCookies.length > 0) {
    await context.addCookies(cleanCookies);
  }

  const page = await context.newPage();
  const models = new Map();
  let fullPayloadCaptured = false;

  // Intercept background API payloads
  page.on('response', async (response) => {
    const contentType = response.headers()['content-type'] || '';
    if (contentType.includes('application/json')) {
      try {
        const data = await response.json();
        let items = [];

        if (Array.isArray(data)) {
          items = data;
        } else if (data && typeof data === 'object') {
          items = data.items || data.objects || data.data || [];
        }

        if (Array.isArray(items) && items.length > 50) {
          console.log(`\n[API Intercepted] URL: ${response.url()}`);
          console.log(`[API Intercepted] Found batch of ${items.length} items.`);

          for (const item of items) {
            const modelId = String(
              item.object_id || 
              (item.object && item.object.id) || 
              item.id || 
              ''
            );
            const modelName = item.name || item.title || (item.object && item.object.name) || `Model ${modelId}`;

            if (modelId) {
              models.set(modelId, modelName);
            }
          }

          if (models.size >= 4000) {
            fullPayloadCaptured = true;
          }
        }
      } catch (err) {}
    }
  });

  console.log('Navigating to MyMiniFactory library...');
  await page.goto('https://www.myminifactory.com/library', { waitUntil: 'domcontentloaded' });

  // Cloudflare Challenge Detection and Grace Period
  try {
    const title = await page.title();
    const hasChallenge = title.includes('Just a moment') || 
                         (await page.$('#challenge-running')) !== null || 
                         (await page.$('#cf-turnstile')) !== null;

    if (hasChallenge) {
      console.log('\n[Cloudflare] Turnstile verification screen detected.');
      console.log('[Cloudflare] Please click the verification box in the browser window if prompted...');

      await page.waitForFunction(() => {
        return !document.title.includes('Just a moment') && !document.querySelector('#challenge-running');
      }, { timeout: 90000 });

      console.log('[Cloudflare] Verification passed. Resuming library capture...');
      
      // Update cookies.json with any newly minted Cloudflare session cookies
      const updatedCookies = await context.cookies();
      fs.writeFileSync('cookies.json', JSON.stringify(updatedCookies, null, 2));
    }
  } catch (cfErr) {
    console.log('[Cloudflare] Wait ended or bypassed: ' + cfErr.message);
  }

  // Wait for initial JSON payloads to stream in
  await page.waitForTimeout(6000);

  if (fullPayloadCaptured) {
    console.log(`\nSuccess! Captured all ${models.size} models directly from API payload.`);
  } else {
    console.log(`\nAPI provided ${models.size} models so far. Proceeding with scroll extraction...`);

    for (let cycle = 1; cycle <= 250; cycle++) {
      const prevCount = models.size;

      const extracted = await page.evaluate(() => {
        function queryShadow(root) {
          let matches = [];
          const nodes = root.querySelectorAll('*');
          for (const node of nodes) {
            if (node.tagName === 'A' && node.href) {
              matches.push({
                href: node.href,
                title: node.innerText || node.getAttribute('title') || ''
              });
            }
            if (node.shadowRoot) {
              matches = matches.concat(queryShadow(node.shadowRoot));
            }
          }
          return matches;
        }
        return queryShadow(document.body);
      });

      for (const item of extracted) {
        const directMatch = item.href.match(/3d-print-.*-(\d+)/);
        if (directMatch) {
          const id = directMatch[1];
          if (!models.has(id) || models.get(id).startsWith('Model ')) {
            models.set(id, item.title.trim() || `Model ${id}`);
          }
        }
      }

      console.log(`Cycle ${cycle}: ${models.size} unique models collected...`);

      await page.evaluate(() => {
        window.scrollTo(0, document.body.scrollHeight);
        const scrollables = Array.from(document.querySelectorAll('*')).filter(el => {
          const style = window.getComputedStyle(el);
          return (style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight;
        });
        scrollables.forEach(s => s.scrollTop = s.scrollHeight);
      });

      await page.keyboard.press('End');
      await page.mouse.wheel(0, 4000);
      await page.waitForTimeout(3000);

      if (models.size >= 4868) {
        console.log(`Target count reached (${models.size} models).`);
        break;
      }

      if (models.size === prevCount && cycle > 5) {
        console.log('No new models detected. Stopping.');
        break;
      }
    }
  }

  // Save outputs
  const sortedIds = Array.from(models.keys()).sort((a, b) => Number(b) - Number(a));
  fs.writeFileSync('model_ids.txt', sortedIds.join('\n') + '\n');
  fs.writeFileSync('models.json', JSON.stringify(Object.fromEntries(models), null, 2));

  console.log(`\nFinished! Saved ${sortedIds.length} model IDs to 'model_ids.txt'.`);
  await browser.close();
})();
