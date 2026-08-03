import "@testing-library/jest-dom/vitest";

// jsdom omits layout APIs the glass-box scroller relies on.
Element.prototype.scrollIntoView = function scrollIntoView() {};
Element.prototype.scrollTo = function scrollTo() {};

