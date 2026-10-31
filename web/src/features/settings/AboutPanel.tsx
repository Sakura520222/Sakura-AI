import { Link } from "react-router-dom";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { BrandMark } from "../../components/common/BrandMark";

export default function AboutPanel() {
  const { t } = useLocale();
  const { version, permissions } = useAuth();
  return (
    <section className="settings-about">
      <div className="settings-category-heading">
        <h2>{t("关于", "About")}</h2>
      </div>
      <div className="settings-about-card">
        <div className="settings-about-heading">
          <div className="settings-about-brand">
            <BrandMark size={32} />
            <div>
              <strong>Sakura AI</strong>
              <span>{version}</span>
            </div>
          </div>
          {permissions.super_admin && (
            <Link className="button primary" to="/admin/version">
              {t("版本更新", "Updates")}
            </Link>
          )}
        </div>
        <nav
          className="settings-about-links"
          aria-label={t("项目链接", "Project links")}
        >
          <a
            href="https://github.com/Sakura520222/Sakura-AI"
            target="_blank"
            rel="noreferrer"
          >
            GitHub
          </a>
          <a
            href="https://sakura520222.github.io/Sakura-AI/"
            target="_blank"
            rel="noreferrer"
          >
            {t("文档", "Documentation")}
          </a>
          <a
            href="https://github.com/Sakura520222/Sakura-AI/releases"
            target="_blank"
            rel="noreferrer"
          >
            {t("更新记录", "Release notes")}
          </a>
        </nav>
      </div>
      <nav
        className="settings-about-legal"
        aria-label={t("法律信息", "Legal information")}
      >
        <Link to="/legal/terms">{t("服务条款", "Terms of service")}</Link>
        <Link to="/legal/privacy">{t("隐私政策", "Privacy policy")}</Link>
        <Link to="/legal/refund">{t("退款政策", "Refund policy")}</Link>
      </nav>
    </section>
  );
}
