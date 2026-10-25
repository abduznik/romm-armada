// Plain-DOM stand-ins for the @decky/ui components the panel uses. They keep the props the panel passes, so a
// test sees what a user would: buttons, inputs, switches, selects.
import React from "react";

export const modals: React.ReactElement[] = [];
export const clearModals = () => (modals.length = 0);

export const PanelSection = ({ title, children }: any) => (
  <section data-title={title ?? ""}>
    {title ? <h3>{title}</h3> : null}
    {children}
  </section>
);
export const PanelSectionRow = ({ children }: any) => <div className="row">{children}</div>;

export const ButtonItem = ({ onClick, disabled, children, description, label }: any) => (
  <div className="item">
    <button disabled={!!disabled} onClick={() => onClick && onClick({})}>
      {children ?? label}
    </button>
    {description ? <small>{description}</small> : null}
  </div>
);

export const Field = ({ label, children }: any) => (
  <div className="field">
    <label>{label}</label>
    <span>{children}</span>
  </div>
);

export const TextField = ({ label, value, onChange, bIsPassword, placeholder, description }: any) => (
  <label className="text">
    <span>{label}</span>
    <input aria-label={label} type={bIsPassword ? "password" : "text"} value={value ?? ""} placeholder={placeholder} onChange={onChange} />
    {description ? <small>{description}</small> : null}
  </label>
);

export const ToggleField = ({ label, checked, onChange, description }: any) => (
  <label className="toggle">
    <span>{label}</span>
    <input aria-label={label} type="checkbox" checked={!!checked} onChange={(e) => onChange && onChange(e.target.checked)} />
    {description ? <small>{description}</small> : null}
  </label>
);

export const DropdownItem = ({ label, rgOptions, selectedOption, onChange, description }: any) => (
  <label className="dropdown">
    <span>{label}</span>
    <select
      aria-label={label}
      value={selectedOption === undefined ? "" : String(selectedOption)}
      onChange={(e) => onChange && onChange(rgOptions.find((o: any) => String(o.data) === e.target.value))}
    >
      <option value="" />
      {rgOptions.map((o: any) => (
        <option key={String(o.data)} value={String(o.data)}>
          {o.label}
        </option>
      ))}
    </select>
    {description ? <small>{description}</small> : null}
  </label>
);

export const SliderField = ({ label, value, min, max, step, onChange, valueSuffix }: any) => (
  <label className="slider">
    <span>{label}</span>
    <input aria-label={label} type="range" value={value} min={min} max={max} step={step} onChange={(e) => onChange && onChange(Number(e.target.value))} />
    <output>{`${value}${valueSuffix ?? ""}`}</output>
  </label>
);

export const ConfirmModal = (_props: any) => null;
export const showModal = (node: React.ReactElement) => {
  modals.push(node);
  return { Close() {}, Update() {} };
};

export const staticClasses = { Title: "title" };
