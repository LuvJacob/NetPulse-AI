/**
 * Sidebar: desktop collapse + mobile slide-out drawer.
 * Toggles classes on #appShell — links stay normal <a href> navigation.
 */
(() => {
  const shell = document.getElementById("appShell");
  const toggle = document.getElementById("sidebarToggle");
  const backdrop = document.getElementById("sidebarBackdrop");

  if (!shell || !toggle) return;

  const mqMobile = window.matchMedia("(max-width: 900px)");

  function isMobileLayout() {
    return mqMobile.matches;
  }

  function closeMobileDrawer() {
    shell.classList.remove("mobile-sidebar-open");
    if (backdrop) {
      backdrop.hidden = true;
      backdrop.setAttribute("aria-hidden", "true");
    }
    document.body.style.overflow = "";
  }

  function openMobileDrawer() {
    shell.classList.add("mobile-sidebar-open");
    if (backdrop) {
      backdrop.hidden = false;
      backdrop.setAttribute("aria-hidden", "false");
    }
    document.body.style.overflow = "hidden";
  }

  toggle.addEventListener("click", () => {
    if (isMobileLayout()) {
      if (shell.classList.contains("mobile-sidebar-open")) {
        closeMobileDrawer();
      } else {
        openMobileDrawer();
      }
    } else {
      shell.classList.toggle("sidebar-collapsed");
      closeMobileDrawer();
    }
  });

  if (backdrop) {
    backdrop.addEventListener("click", closeMobileDrawer);
  }

  /** Resize: avoid stuck open drawer state when crossing breakpoints */
  mqMobile.addEventListener("change", () => {
    if (!isMobileLayout()) {
      closeMobileDrawer();
    }
  });

  /** Esc closes mobile drawer */
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && shell.classList.contains("mobile-sidebar-open")) {
      closeMobileDrawer();
    }
  });
})();
