let selectSequence = 0;
let openSelect = null;

function labelFor(select) {
    const labelledBy = select.getAttribute('aria-labelledby');
    if (labelledBy) {
        return labelledBy
            .split(/\s+/)
            .map((id) => document.getElementById(id)?.textContent?.trim())
            .filter(Boolean)
            .join(' ');
    }
    return select.getAttribute('aria-label') || '选择选项';
}

function enhanceSelect(select) {
    if (select.dataset.customSelectReady === 'true') return;
    select.dataset.customSelectReady = 'true';

    const wrapper = document.createElement('div');
    const trigger = document.createElement('button');
    const value = document.createElement('span');
    const arrow = document.createElement('span');
    const menu = document.createElement('div');
    const menuId = `custom-select-menu-${++selectSequence}`;

    wrapper.className = 'custom-select';
    trigger.type = 'button';
    trigger.className = 'custom-select-trigger';
    trigger.setAttribute('role', 'combobox');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', menuId);
    trigger.setAttribute('aria-label', labelFor(select));
    value.className = 'custom-select-value';
    arrow.className = 'custom-select-arrow';
    arrow.setAttribute('aria-hidden', 'true');
    arrow.textContent = '⌄';
    menu.className = 'custom-select-menu';
    menu.id = menuId;
    menu.setAttribute('role', 'listbox');
    menu.hidden = true;

    select.before(wrapper);
    wrapper.append(select, trigger);
    document.body.append(menu);
    select.classList.add('custom-select-native');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    trigger.append(value, arrow);

    function options() {
        return [...select.options].filter((option) => !option.disabled);
    }

    function sync() {
        const selected = select.selectedOptions[0];
        value.textContent = selected?.textContent?.trim() || '请选择';
        trigger.disabled = select.disabled || options().length === 0;
        trigger.setAttribute('aria-label', `${labelFor(select)}：${value.textContent}`);
        for (const optionButton of menu.querySelectorAll('[role="option"]')) {
            const isSelected = optionButton.dataset.value === select.value;
            optionButton.setAttribute('aria-selected', String(isSelected));
        }
    }

    function rebuild() {
        menu.replaceChildren();
        for (const option of options()) {
            const optionButton = document.createElement('button');
            optionButton.type = 'button';
            optionButton.className = 'custom-select-option';
            optionButton.dataset.value = option.value;
            optionButton.textContent = option.textContent;
            optionButton.setAttribute('role', 'option');
            optionButton.setAttribute('aria-selected', String(option.selected));
            optionButton.addEventListener('click', () => {
                select.value = option.value;
                select.dispatchEvent(new Event('change', {bubbles: true}));
                close();
                trigger.focus();
            });
            menu.append(optionButton);
        }
        sync();
    }

    function close() {
        menu.hidden = true;
        wrapper.classList.remove('is-open');
        trigger.setAttribute('aria-expanded', 'false');
        if (openSelect === close) openSelect = null;
    }

    function open(focusSelected = false) {
        if (trigger.disabled) return;
        if (openSelect && openSelect !== close) openSelect();
        rebuild();
        menu.hidden = false;
        const triggerRect = trigger.getBoundingClientRect();
        menu.style.minWidth = `${triggerRect.width}px`;
        const menuRect = menu.getBoundingClientRect();
        const left = Math.min(triggerRect.left, window.innerWidth - menuRect.width - 8);
        const openBelow = window.innerHeight - triggerRect.bottom >= menuRect.height + 8;
        menu.style.left = `${Math.max(8, left)}px`;
        menu.style.top = openBelow
            ? `${triggerRect.bottom + 6}px`
            : `${Math.max(8, triggerRect.top - menuRect.height - 6)}px`;
        wrapper.classList.add('is-open');
        trigger.setAttribute('aria-expanded', 'true');
        openSelect = close;
        if (focusSelected) {
            const selected = menu.querySelector('[aria-selected="true"]');
            (selected || menu.querySelector('[role="option"]'))?.focus();
        }
    }

    trigger.addEventListener('click', () => {
        if (menu.hidden) open();
        else close();
    });
    trigger.addEventListener('keydown', (event) => {
        if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
            event.preventDefault();
            open(true);
        }
    });
    menu.addEventListener('keydown', (event) => {
        const optionButtons = [...menu.querySelectorAll('[role="option"]')];
        const current = optionButtons.indexOf(document.activeElement);
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
            trigger.focus();
            return;
        }
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let next = current;
        if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = optionButtons.length - 1;
        else if (event.key === 'ArrowDown') next = Math.min(current + 1, optionButtons.length - 1);
        else next = Math.max(current - 1, 0);
        optionButtons[next]?.focus();
    });

    select.addEventListener('change', sync);
    new MutationObserver(() => {
        rebuild();
        if (!menu.hidden && trigger.disabled) close();
    }).observe(select, {childList: true, subtree: true, attributes: true});

    rebuild();
}

export function initializeCustomSelects(root = document) {
    root.querySelectorAll('select[data-custom-select]').forEach(enhanceSelect);
    document.addEventListener('pointerdown', (event) => {
        if (openSelect && !event.target.closest('.custom-select, .custom-select-menu')) openSelect();
    });
    window.addEventListener('resize', () => openSelect?.());
    document.addEventListener('scroll', () => openSelect?.(), true);
}
