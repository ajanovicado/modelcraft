import { createContext } from "react";

/** Data colors of the draft's report theme, so local previews match the Power BI build. */
export const ReportPaletteContext = createContext<string[] | undefined>(undefined);
