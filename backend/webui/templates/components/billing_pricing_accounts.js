/* Model discovery stays scoped to a configured account; no browser credentials. */
function billingPricingAccountPicker(form, accounts, labels, auxiliarySources = []) {
    const accountField = form.elements.namedItem('account_id');
    const modelField = form.elements.namedItem('model_id');
    const scopeField = form.elements.namedItem('source_scope');
    const kindField = form.elements.namedItem('call_kind');
    const status = document.getElementById('pricing-model-status');
    const refreshButton = document.getElementById('pricing-refresh-models');
    let generation = 0;
    let controller = null;
    let accountKind = ['embedding', 'rerank'].includes(kindField.value) ? 'chat' : kindField.value;
    const modelsByAccount = new Map();

    function normalizeModels(items) {
        const result = [];
        const seen = new Set();
        for (const item of Array.isArray(items) ? items : []) {
            const id = typeof item === 'string' ? item : item && (item.id || item.model_id);
            if (typeof id !== 'string' || !id.trim() || seen.has(id)) continue;
            seen.add(id);
            result.push({id: id, label: typeof item === 'object' && typeof item.label === 'string' ? item.label : id});
        }
        return result;
    }

    function configuredModels(account) {
        return normalizeModels([...(account.models || []), account.default_model || '']);
    }

    function selectedAccount() {
        if (scopeField && scopeField.value !== 'account') return undefined;
        return accounts.find(account => account.id === accountField.value);
    }

    function setStatus(message, state) {
        status.textContent = message;
        status.dataset.state = state;
        status.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
    }

    function renderModels(account, models, requested) {
        modelField.replaceChildren();
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = labels.chooseModel;
        modelField.appendChild(placeholder);
        for (const model of models) {
            const option = document.createElement('option');
            option.value = model.id;
            option.textContent = model.label;
            modelField.appendChild(option);
        }
        if (requested && !models.some(model => model.id === requested)) {
            // Preserve a historical/submitted model visibly without accepting it as available.
            const obsolete = document.createElement('option');
            obsolete.value = requested;
            obsolete.textContent = requested + ' · ' + labels.unavailable;
            obsolete.disabled = true;
            modelField.appendChild(obsolete);
        }
        const preferred = requested || (account && account.default_model) || '';
        modelField.value = preferred;
        modelField.disabled = !account;
        refreshButton.disabled = !account;
    }

    return {
        init() {
            accountField.addEventListener('change', () => this.changeAccount());
            refreshButton.addEventListener('click', () => this.refresh(true));
            if (scopeField) scopeField.addEventListener('change', () => this.changeSource(true));
            this.changeSource(false);
        },
        changeSource(resetSelection) {
            generation += 1;
            if (controller) controller.abort();
            const scope = scopeField ? scopeField.value : 'account';
            if (scope !== 'account') {
                if (resetSelection && !accountField.disabled) accountKind = kindField.value;
                const source = auxiliarySources.find(item => item.feature === scope);
                accountField.disabled = true;
                accountField.required = false;
                modelField.replaceChildren();
                if (source) {
                    const option = document.createElement('option');
                    option.value = source.model_id;
                    option.textContent = source.model_id;
                    modelField.appendChild(option);
                    modelField.value = source.model_id;
                }
                modelField.disabled = true;
                modelField.required = false;
                kindField.value = source ? source.feature : '';
                kindField.disabled = true;
                kindField.required = false;
                refreshButton.hidden = true;
                setStatus(source ? labels.auxiliary : labels.auxiliaryUnavailable, source ? 'ready' : 'error');
                return;
            }
            accountField.disabled = false;
            accountField.required = true;
            modelField.required = true;
            kindField.disabled = false;
            kindField.required = true;
            refreshButton.hidden = false;
            if (resetSelection) {
                accountField.value = '';
                modelField.value = '';
                kindField.value = accountKind;
            }
            const account = selectedAccount();
            if (!account) {
                renderModels(null, [], modelField.value);
                setStatus(labels.chooseAccount, 'empty');
                return;
            }
            renderModels(account, configuredModels(account), modelField.value);
            this.refresh();
        },
        changeAccount() {
            generation += 1;
            if (controller) controller.abort();
            const account = selectedAccount();
            const models = account ? (modelsByAccount.get(account.id) || configuredModels(account)) : [];
            renderModels(account, models, '');
            if (!account) {
                setStatus(labels.chooseAccount, 'empty');
                return;
            }
            this.refresh();
        },
        async refresh(force = false) {
            const account = selectedAccount();
            if (!account) return;
            const accountId = account.id;
            const currentGeneration = ++generation;
            if (controller) controller.abort();
            controller = new AbortController();
            setStatus(labels.loading, 'loading');
            refreshButton.disabled = true;
            try {
                const url = '/billing/admin/pricing/accounts/' + encodeURIComponent(accountId) + '/models' + (force ? '?refresh=true' : '');
                const response = await fetch(url, {
                    method: 'GET', credentials: 'same-origin', headers: {Accept: 'application/json'}, signal: controller.signal,
                });
                const payload = await response.json();
                if (currentGeneration !== generation || accountField.value !== accountId) return;
                if (!response.ok || !payload.success || !payload.data ||
                        (payload.data.account_id && payload.data.account_id !== accountId) ||
                        !Array.isArray(payload.data.models)) throw new Error('Model discovery failed');
                const models = normalizeModels([...configuredModels(account), ...payload.data.models]);
                modelsByAccount.set(accountId, models);
                renderModels(account, models, modelField.value);
                if (payload.data.discovery_failed) setStatus(labels.failed, 'error');
                else setStatus(models.length ? labels.ready.replace('{count}', String(models.length)) : labels.noModels,
                    models.length ? 'ready' : 'empty');
            } catch (error) {
                if (currentGeneration !== generation || accountField.value !== accountId) return;
                // Never surface provider errors which might contain URLs or credentials.
                setStatus(labels.failed, 'error');
            } finally {
                if (currentGeneration === generation && accountField.value === accountId) refreshButton.disabled = false;
            }
        },
    };
}
