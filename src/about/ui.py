import gradio as gr

from ..core.i18n import translator


def build_about_tab(language: str):
    t = translator(language).t
    return gr.HTML(
        f"""
        <section class="about-details" aria-labelledby="about-name">
            <h2 id="about-name">{t("about.title")}</h2>
            <p class="about-version">{t("about.version")}</p>
            <div class="about-links">
                <p>
                    <a href="https://github.com/Merserk/dlss5-visual-enhancer"
                       target="_blank" rel="noopener noreferrer">GitHub</a>
                    <span class="about-description">{t("about.github_description")}</span>
                </p>
                <p>
                    <a href="https://www.patreon.com/Merserk"
                       target="_blank" rel="noopener noreferrer">Patreon</a>
                    <span class="about-description">{t("about.patreon_description")}</span>
                </p>
            </div>
            <p class="about-copyright">&copy; Merserk</p>
        </section>
        """,
        elem_id="about-content",
        padding=False,
    )
