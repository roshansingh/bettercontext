import { createRequire } from "module";
import fs from "fs";
import path from "path";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const repoRoot = input.repoRoot;
const files = input.files;

function loadTypeScript() {
  try {
    return createRequire(path.join(repoRoot, "package.json"))("typescript");
  } catch {
    return createRequire(import.meta.url)("typescript");
  }
}

const ts = loadTypeScript();

function scriptKind(filePath) {
  if (filePath.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (filePath.endsWith(".jsx")) return ts.ScriptKind.JSX;
  if (filePath.endsWith(".js") || filePath.endsWith(".mjs") || filePath.endsWith(".cjs")) return ts.ScriptKind.JS;
  return ts.ScriptKind.TS;
}

function lineOf(sourceFile, pos) {
  return sourceFile.getLineAndCharacterOfPosition(pos).line + 1;
}

function textOf(node, sourceFile) {
  return node.getText(sourceFile);
}

function callName(expr, sourceFile) {
  if (ts.isIdentifier(expr)) return expr.text;
  if (ts.isPropertyAccessExpression(expr)) {
    const left = callName(expr.expression, sourceFile);
    return left ? `${left}.${expr.name.text}` : null;
  }
  return null;
}

function bindingNames(name) {
  if (ts.isIdentifier(name)) return [name.text];
  if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
    return name.elements.flatMap((element) => {
      if (ts.isBindingElement(element)) return bindingNames(element.name);
      return [];
    });
  }
  return [];
}

function isFunctionBoundary(node) {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isConstructorDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node)
  );
}

function isThisRebindingFunctionBoundary(node) {
  return isFunctionBoundary(node) && !ts.isArrowFunction(node);
}

function collectImports(sourceFile) {
  const imports = [];
  for (const statement of sourceFile.statements) {
    if (ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier)) {
      const importedNames = [];
      const localNames = [];
      const clause = statement.importClause;
      if (clause?.name) {
        importedNames.push("default");
        localNames.push(clause.name.text);
      }
      if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
        importedNames.push(clause.namedBindings.name.text);
        localNames.push(clause.namedBindings.name.text);
      }
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const element of clause.namedBindings.elements) {
          importedNames.push((element.propertyName ?? element.name).text);
          localNames.push(element.name.text);
        }
      }
      imports.push({
        raw_target: statement.moduleSpecifier.text,
        line: lineOf(sourceFile, statement.getStart(sourceFile)),
        imported_names: importedNames,
        local_names: localNames,
        is_type_only: Boolean(clause?.isTypeOnly),
      });
      continue;
    }

    if (ts.isExportDeclaration(statement) && statement.moduleSpecifier && ts.isStringLiteral(statement.moduleSpecifier)) {
      imports.push({
        raw_target: statement.moduleSpecifier.text,
        line: lineOf(sourceFile, statement.getStart(sourceFile)),
        imported_names: [],
        local_names: [],
        is_type_only: Boolean(statement.isTypeOnly),
      });
      continue;
    }

    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        const init = declaration.initializer;
        if (
          init &&
          ts.isCallExpression(init) &&
          ts.isIdentifier(init.expression) &&
          init.expression.text === "require" &&
          init.arguments.length === 1 &&
          ts.isStringLiteral(init.arguments[0])
        ) {
          const names = bindingNames(declaration.name);
          imports.push({
            raw_target: init.arguments[0].text,
            line: lineOf(sourceFile, statement.getStart(sourceFile)),
            imported_names: names,
            local_names: names,
            is_type_only: false,
          });
        }
      }
    }
  }
  return imports;
}

const HTTP_METHODS = new Set(["get", "post", "put", "delete", "patch", "options", "head"]);
const HTTP_WRAPPER_METHODS = new Map([
  ["get", "GET"],
  ["post", "POST"],
  ["put", "PUT"],
  ["patch", "PATCH"],
  ["delete", "DELETE"],
  ["del", "DELETE"],
  ["head", "HEAD"],
  ["options", "OPTIONS"],
  ["fetch", "ANY"],
  ["request", "ANY"],
]);
const ENDPOINT_CONFIG_TARGET_PROPERTIES = ["path", "url"];
const ENDPOINT_CONFIG_BASE_URL_PROPERTIES = ["baseURL", "baseUrl", "base_url"];
const ENDPOINT_HOST_BASE_IDENTIFIER_TOKENS = new Set(["url", "uri", "host", "hostname", "origin"]);
const ASSIGNMENT_OPERATORS = new Set([
  ts.SyntaxKind.EqualsToken,
  ts.SyntaxKind.PlusEqualsToken,
  ts.SyntaxKind.MinusEqualsToken,
  ts.SyntaxKind.AsteriskEqualsToken,
  ts.SyntaxKind.AsteriskAsteriskEqualsToken,
  ts.SyntaxKind.SlashEqualsToken,
  ts.SyntaxKind.PercentEqualsToken,
  ts.SyntaxKind.LessThanLessThanEqualsToken,
  ts.SyntaxKind.GreaterThanGreaterThanEqualsToken,
  ts.SyntaxKind.GreaterThanGreaterThanGreaterThanEqualsToken,
  ts.SyntaxKind.AmpersandEqualsToken,
  ts.SyntaxKind.BarEqualsToken,
  ts.SyntaxKind.CaretEqualsToken,
  ts.SyntaxKind.AmpersandAmpersandEqualsToken,
  ts.SyntaxKind.BarBarEqualsToken,
  ts.SyntaxKind.QuestionQuestionEqualsToken,
]);
const EXPRESS_ROUTE_METHODS = new Set([...HTTP_METHODS, "all"]);
const KOA_ROUTE_METHODS = new Set([...EXPRESS_ROUTE_METHODS, "del"]);
const SOURCE_FILE_DECLARATION_NAMES = new WeakMap();
const HOISTED_VAR_DECLARATION_NAMES = new WeakMap();
const SOURCE_FILE_LITERAL_ALIASES = new WeakMap();

function stringLiteralValue(node) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  return null;
}

function rawNodeText(node, sourceFile) {
  const text = node.getText(sourceFile);
  return text.length > 80 ? `${text.slice(0, 77)}...` : text;
}

function expressionResult(kind, value, raw, resolutionKind = null) {
  const result = { kind, value, raw };
  if (resolutionKind != null) result.resolution_kind = resolutionKind;
  return result;
}

function routeParamNameFromExpression(node) {
  if (ts.isIdentifier(node)) return node.text;
  if (ts.isPropertyAccessExpression(node)) return node.name.text;
  if (ts.isElementAccessExpression(node) && ts.isStringLiteral(node.argumentExpression)) return node.argumentExpression.text;
  return null;
}

function isSafeRouteParamName(name) {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name);
}

function uniqueStrings(values) {
  return Array.from(new Set(values));
}

function objectFromEntriesWithoutPrototype(entries) {
  const result = Object.create(null);
  for (const [key, value] of entries) {
    result[key] = value;
  }
  return result;
}

function hasSafeTemplatePathFrame(value, options = {}) {
  if (value.startsWith("/")) return true;
  if (value.startsWith("${env:")) {
    const hostEnd = value.indexOf("}");
    return hostEnd >= 0 && value.slice(hostEnd + 1).startsWith("/");
  }
  return options.relativePathAllowed === true && value.length > 0;
}

function templateParameterizationFailure(value, followingText, hasMoreSpans, options = {}) {
  if (value.length === 0 || !hasSafeTemplatePathFrame(value, options)) return "template_dynamic_host_position";
  if (!value.endsWith("/")) return "template_dynamic_composite_segment";
  if (followingText.length === 0) return hasMoreSpans ? "template_dynamic_composite_segment" : null;
  return followingText.startsWith("/") || followingText.startsWith("?") || followingText.startsWith("#")
    ? null
    : "template_dynamic_composite_segment";
}

function isReferenceLikeExpression(node) {
  if (!node) return false;
  if (ts.isIdentifier(node) || node.kind === ts.SyntaxKind.ThisKeyword || node.kind === ts.SyntaxKind.SuperKeyword) return true;
  if (ts.isPropertyAccessExpression(node)) return isReferenceLikeExpression(node.expression);
  if (ts.isElementAccessExpression(node)) return isReferenceLikeExpression(node.expression);
  if (node.kind === ts.SyntaxKind.ParenthesizedExpression && node.expression) return isReferenceLikeExpression(node.expression);
  return false;
}

function canTreatTemplateSpanAsUnresolvedHost(value, followingText, spanIndex, expression) {
  // The emitted endpoint path is the static suffix after an unknown base/host; confidence and coverage remain low-trust.
  return spanIndex === 0 && value.length === 0 && followingText.startsWith("/") && isReferenceLikeExpression(expression);
}

function identifierNameTokens(name) {
  const tokens = [];
  let current = "";
  for (let index = 0; index < name.length; index += 1) {
    const char = name[index];
    if (char === "_" || char === "-" || char === ".") {
      if (current.length > 0) tokens.push(current.toLowerCase());
      current = "";
      continue;
    }
    const isUpper = char >= "A" && char <= "Z";
    const previous = current[current.length - 1] ?? "";
    const next = name[index + 1] ?? "";
    const previousIsLowerOrDigit = (previous >= "a" && previous <= "z") || (previous >= "0" && previous <= "9");
    const nextIsLower = next >= "a" && next <= "z";
    if (isUpper && current.length > 0 && (previousIsLowerOrDigit || nextIsLower)) {
      tokens.push(current.toLowerCase());
      current = char;
      continue;
    }
    current += char;
  }
  if (current.length > 0) tokens.push(current.toLowerCase());
  return tokens;
}

function identifierNameLooksLikeEndpointHostBase(name) {
  return identifierNameTokens(name).some((token) => ENDPOINT_HOST_BASE_IDENTIFIER_TOKENS.has(token));
}

function bindingPropertyNameText(node) {
  if (!node) return null;
  if (ts.isIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node)) return node.text;
  return null;
}

function bindingPropertyNamesForTarget(nameNode, targetName) {
  const propertyNames = [];
  function visit(node, inheritedPropertyName = null) {
    if (ts.isIdentifier(node)) {
      if (node.text === targetName) propertyNames.push(inheritedPropertyName ?? node.text);
      return;
    }
    if (ts.isObjectBindingPattern(node)) {
      for (const element of node.elements) {
        const propertyName = bindingPropertyNameText(element.propertyName) ?? (ts.isIdentifier(element.name) ? element.name.text : null);
        visit(element.name, propertyName ?? inheritedPropertyName);
      }
      return;
    }
    if (ts.isArrayBindingPattern(node)) {
      for (const element of node.elements) {
        if (ts.isBindingElement(element)) visit(element.name, inheritedPropertyName);
      }
    }
  }
  visit(nameNode);
  return propertyNames;
}

function objectBindingInfoForLocalIdentifierUse(identifier, sourceFile) {
  if (!ts.isIdentifier(identifier)) return null;
  const targetName = identifier.text;
  const useStart = identifier.getStart(sourceFile);
  function groupForBindingName(nameNode) {
    if (!ts.isObjectBindingPattern(nameNode)) return null;
    const propertyNames = bindingPropertyNamesForTarget(nameNode, targetName);
    if (propertyNames.length === 0) return null;
    return { group: `${nameNode.getStart(sourceFile)}:${nameNode.getEnd()}`, propertyNames };
  }
  function nodeContainsUse(node) {
    return node.getStart(sourceFile) <= useStart && useStart < node.getEnd();
  }
  function isLexicalContainer(node) {
    return (
      ts.isBlock(node) ||
      ts.isCaseClause(node) ||
      ts.isDefaultClause(node) ||
      ts.isModuleBlock(node) ||
      ts.isForStatement(node) ||
      ts.isForInStatement(node) ||
      ts.isForOfStatement(node) ||
      ts.isCatchClause(node)
    );
  }
  let found = null;
  function visit(node) {
    if (node.getStart(sourceFile) > useStart) return;
    if (node !== sourceFile && isLexicalContainer(node) && !nodeContainsUse(node)) return;
    if (node !== sourceFile && (isFunctionBoundary(node) || ts.isClassDeclaration(node) || ts.isClassExpression(node))) {
      if (!nodeContainsUse(node)) return;
      for (const parameter of node.parameters ?? []) {
        const group = groupForBindingName(parameter.name);
        if (group != null) found = group;
      }
    }
    if (ts.isVariableDeclaration(node) && node.getStart(sourceFile) < useStart) {
      const group = groupForBindingName(node.name);
      if (group != null) found = group;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

function canTreatTemplateSpanAsLeadingRelativePathParam(value, followingText, spanIndex, expression, templateNode, sourceFile, options = {}) {
  if (options.leadingDynamicRelativePathAllowed !== true) return false;
  if (spanIndex !== 0 || value.length !== 0 || followingText !== "/") return false;
  if (!ts.isIdentifier(expression) || !identifierHasLocalScopedDeclaration(expression, expression.text, sourceFile)) return false;
  if (rawIdentifierCanResolveThroughImport(expression, sourceFile)) return false;
  if (templateNode.templateSpans.length < 2) return false;
  const nextSpan = templateNode.templateSpans[1];
  if (!ts.isIdentifier(nextSpan.expression)) return false;
  if (!identifierHasLocalScopedDeclaration(nextSpan.expression, nextSpan.expression.text, sourceFile)) return false;
  if (rawIdentifierCanResolveThroughImport(nextSpan.expression, sourceFile)) return false;
  // Local fetch wrappers usually provide the base URL; paired object-context fields are route params, not hosts.
  const leadingInfo = objectBindingInfoForLocalIdentifierUse(expression, sourceFile);
  const nextInfo = objectBindingInfoForLocalIdentifierUse(nextSpan.expression, sourceFile);
  if (leadingInfo == null || nextInfo == null || leadingInfo.group !== nextInfo.group) return false;
  if (
    identifierNameLooksLikeEndpointHostBase(expression.text) ||
    leadingInfo.propertyNames.some((propertyName) => identifierNameLooksLikeEndpointHostBase(propertyName))
  ) {
    return false;
  }
  return (
    nextSpan.literal.text.length > 1 &&
    (nextSpan.literal.text.startsWith("/") || nextSpan.literal.text.startsWith("?") || nextSpan.literal.text.startsWith("#"))
  );
}

function topLevelLiteralBindingValue(node, bindings, invalid, depth = 0) {
  if (depth > 32) return null;
  const literal = stringLiteralValue(node);
  if (literal != null) return literal;
  if (node.kind === ts.SyntaxKind.ParenthesizedExpression && node.expression) {
    return topLevelLiteralBindingValue(node.expression, bindings, invalid, depth + 1);
  }
  if (ts.isIdentifier(node)) return !invalid.has(node.text) && bindings.has(node.text) ? bindings.get(node.text) : null;
  if (ts.isTemplateExpression(node)) {
    let value = node.head.text;
    for (const span of node.templateSpans) {
      const resolved = topLevelLiteralBindingValue(span.expression, bindings, invalid, depth + 1);
      if (resolved == null) return null;
      value += resolved;
      value += span.literal.text;
    }
    return value;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = topLevelLiteralBindingValue(node.left, bindings, invalid, depth + 1);
    const right = topLevelLiteralBindingValue(node.right, bindings, invalid, depth + 1);
    return left != null && right != null ? `${left}${right}` : null;
  }
  return null;
}

function collectTopLevelLiteralBindings(sourceFile) {
  const bindings = new Map();
  const invalid = new Set();
  const declared = new Set();
  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement)) {
      if (nodeHasDeclareModifier(statement)) continue;
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name)) continue;
        const name = declaration.name.text;
        if (declared.has(name)) {
          invalid.add(name);
          bindings.delete(name);
        }
        declared.add(name);
        const literal = declaration.initializer ? topLevelLiteralBindingValue(declaration.initializer, bindings, invalid) : null;
        if (literal == null || invalid.has(name)) {
          // Unsupported initializers are skipped; duplicate poisoning is tracked by declared.
          continue;
        }
        bindings.set(name, literal);
      }
      for (const name of bindings.keys()) {
        if (statementMutatesIdentifier(statement, name)) {
          invalid.add(name);
          bindings.delete(name);
        }
      }
      continue;
    }
    for (const name of bindings.keys()) {
      if (statementMutatesIdentifier(statement, name)) {
        invalid.add(name);
        bindings.delete(name);
      }
    }
  }
  for (const name of invalid) bindings.delete(name);
  return bindings;
}

function collectTopLevelConstLiteralBindings(sourceFile) {
  const bindings = new Map();
  const invalid = new Set();
  const declared = new Set();
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    if (nodeHasDeclareModifier(statement)) continue;
    if (!(statement.declarationList.flags & ts.NodeFlags.Const)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name)) continue;
      const name = declaration.name.text;
      if (declared.has(name)) {
        invalid.add(name);
        bindings.delete(name);
      }
      declared.add(name);
      const literal = declaration.initializer ? topLevelLiteralBindingValue(declaration.initializer, bindings, invalid) : null;
      if (literal == null || invalid.has(name)) continue;
      bindings.set(name, literal);
    }
  }
  for (const name of invalid) bindings.delete(name);
  return bindings;
}

function collectTopLevelLiteralAliases(sourceFile) {
  const cached = SOURCE_FILE_LITERAL_ALIASES.get(sourceFile);
  if (cached) return cached;
  const aliases = new Map();
  const invalid = new Set();
  const declared = new Set();
  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement)) {
      if (nodeHasDeclareModifier(statement)) continue;
      const isConst = Boolean(statement.declarationList.flags & ts.NodeFlags.Const);
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name)) continue;
        const name = declaration.name.text;
        if (declared.has(name)) {
          invalid.add(name);
          aliases.delete(name);
        }
        declared.add(name);
        if (isConst && declaration.initializer && ts.isIdentifier(declaration.initializer) && !invalid.has(name)) {
          aliases.set(name, declaration.initializer.text);
        }
      }
      for (const name of aliases.keys()) {
        if (statementMutatesIdentifier(statement, name)) {
          invalid.add(name);
          aliases.delete(name);
        }
      }
      continue;
    }
    for (const name of aliases.keys()) {
      if (statementMutatesIdentifier(statement, name)) {
        invalid.add(name);
        aliases.delete(name);
      }
    }
  }
  for (const name of invalid) aliases.delete(name);
  const result = objectFromEntriesWithoutPrototype(aliases);
  SOURCE_FILE_LITERAL_ALIASES.set(sourceFile, result);
  return result;
}

function rawIdentifierCanResolveThroughImport(node, sourceFile) {
  if (!ts.isIdentifier(node)) return false;
  const targetName = node.text;
  if (identifierHasLocalScopedDeclaration(node, targetName, sourceFile)) return false;
  if (sourceFileImportDeclaresName(sourceFile, targetName)) return true;
  const aliases = collectTopLevelLiteralAliases(sourceFile);
  const seen = new Set([targetName]);
  let current = aliases[targetName];
  while (typeof current === "string" && !seen.has(current)) {
    if (sourceFileImportDeclaresName(sourceFile, current)) return true;
    seen.add(current);
    current = aliases[current];
  }
  return false;
}

function collectLiteralExports(sourceFile) {
  const literalExports = Object.create(null);
  const constBindings = collectTopLevelConstLiteralBindings(sourceFile);
  function addExport(exportName, localName) {
    if (constBindings.has(localName)) literalExports[exportName] = constBindings.get(localName);
  }

  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement) && statementHasExportModifier(statement) && !nodeHasDeclareModifier(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name)) addExport(declaration.name.text, declaration.name.text);
      }
      continue;
    }

    if (ts.isExportDeclaration(statement) && !statement.moduleSpecifier && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      for (const element of statement.exportClause.elements) {
        const localName = (element.propertyName ?? element.name).text;
        addExport(element.name.text, localName);
      }
      continue;
    }

    if (ts.isExportAssignment(statement)) {
      const literal = topLevelLiteralBindingValue(statement.expression, constBindings, new Set());
      if (literal != null) literalExports.default = literal;
    }
  }
  return literalExports;
}

function declaresNameInBindingName(nameNode, targetName) {
  return bindingNames(nameNode).includes(targetName);
}

function parametersDeclareName(node, targetName) {
  return node.parameters?.some((param) => declaresNameInBindingName(param.name, targetName)) ?? false;
}

function variableStatementDeclaresName(statement, targetName) {
  if (!ts.isVariableStatement(statement)) return false;
  if (nodeHasDeclareModifier(statement)) return false;
  return statement.declarationList.declarations.some((declaration) => declaresNameInBindingName(declaration.name, targetName));
}

function forInitializerDeclaresName(initializer, targetName) {
  if (!initializer || !ts.isVariableDeclarationList(initializer)) return false;
  return initializer.declarations.some((declaration) => declaresNameInBindingName(declaration.name, targetName));
}

function forInOfInitializerDeclaresName(initializer, targetName) {
  if (ts.isVariableDeclarationList(initializer)) {
    return initializer.declarations.some((declaration) => declaresNameInBindingName(declaration.name, targetName));
  }
  return declaresNameInBindingName(initializer, targetName);
}

function sourceFileImportDeclaresName(sourceFile, targetName) {
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const clause = statement.importClause;
    if (!clause) continue;
    if (clause.isTypeOnly) continue;
    if (clause.name?.text === targetName) return true;
    const bindings = clause.namedBindings;
    if (bindings && ts.isNamespaceImport(bindings) && bindings.name.text === targetName) return true;
    if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        if (element.isTypeOnly) continue;
        if (element.name.text === targetName) return true;
      }
    }
  }
  return false;
}

function sourceFileDeclaredNames(sourceFile) {
  const cached = SOURCE_FILE_DECLARATION_NAMES.get(sourceFile);
  if (cached) return cached;
  const names = new Set();
  for (const statement of sourceFile.statements) {
    for (const name of statementDeclaredNames(statement)) {
      names.add(name);
    }
  }
  SOURCE_FILE_DECLARATION_NAMES.set(sourceFile, names);
  return names;
}

function sourceFileDeclaresName(sourceFile, targetName) {
  return sourceFileDeclaredNames(sourceFile).has(targetName);
}

function nodeHasModifier(node, kind) {
  return Array.from(node.modifiers ?? []).some((modifier) => modifier.kind === kind);
}

function isClassStaticBlock(node) {
  return typeof ts.isClassStaticBlockDeclaration === "function" && ts.isClassStaticBlockDeclaration(node);
}

function isStaticClassElement(node) {
  if (isClassStaticBlock(node)) return true;
  if (
    ts.isMethodDeclaration(node) ||
    ts.isPropertyDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node)
  ) {
    return nodeHasModifier(node, ts.SyntaxKind.StaticKeyword);
  }
  return false;
}

function nodeHasDeclareModifier(node) {
  return nodeHasModifier(node, ts.SyntaxKind.DeclareKeyword);
}

function declarationListIsVar(declarationList) {
  return (declarationList.flags & (ts.NodeFlags.Let | ts.NodeFlags.Const)) === 0;
}

function isHoistedVarBoundary(node) {
  return isFunctionBoundary(node) || ts.isClassDeclaration(node) || ts.isClassExpression(node) || ts.isModuleDeclaration(node);
}

function hoistedVarDeclaredNames(scopeNode) {
  const cached = HOISTED_VAR_DECLARATION_NAMES.get(scopeNode);
  if (cached) return cached;
  const names = new Set();
  function visit(node) {
    if (node !== scopeNode && isHoistedVarBoundary(node)) return;
    if (node !== scopeNode && nodeHasDeclareModifier(node)) return;
    if (ts.isVariableDeclarationList(node) && declarationListIsVar(node)) {
      for (const declaration of node.declarations) {
        for (const name of bindingNames(declaration.name)) {
          names.add(name);
        }
      }
    }
    node.forEachChild(visit);
  }
  visit(scopeNode);
  HOISTED_VAR_DECLARATION_NAMES.set(scopeNode, names);
  return names;
}

function hoistedVarDeclaresName(scopeNode, targetName) {
  return hoistedVarDeclaredNames(scopeNode).has(targetName);
}

function blockDeclaresNameBeforeUse(block, targetName, useNode, sourceFile) {
  for (const statement of block.statements ?? []) {
    if (statement.getStart(sourceFile) > useNode.getStart(sourceFile)) break;
    if (variableStatementDeclaresName(statement, targetName)) return true;
    if (
      ts.isFunctionDeclaration(statement) &&
      statement.name?.text === targetName &&
      statement.getStart(sourceFile) < useNode.getStart(sourceFile)
    ) {
      return true;
    }
  }
  return false;
}

function identifierIsLocallyShadowed(useNode, targetName, sourceFile) {
  let current = useNode.parent;
  while (current && current !== sourceFile) {
    if (
      isFunctionBoundary(current) &&
      parametersDeclareName(current, targetName)
    ) {
      return true;
    }
    if (ts.isBlock(current) && blockDeclaresNameBeforeUse(current, targetName, useNode, sourceFile)) return true;
    if (ts.isForStatement(current) && forInitializerDeclaresName(current.initializer, targetName)) return true;
    if ((ts.isForOfStatement(current) || ts.isForInStatement(current)) && forInOfInitializerDeclaresName(current.initializer, targetName)) return true;
    if (ts.isCatchClause(current) && current.variableDeclaration && declaresNameInBindingName(current.variableDeclaration.name, targetName)) return true;
    if (ts.isWithStatement(current)) return true;
    current = current.parent;
  }
  return false;
}

function nodeHasWithAncestor(node, sourceFile) {
  let current = node.parent;
  while (current && current !== sourceFile) {
    if (ts.isWithStatement(current)) return true;
    current = current.parent;
  }
  return false;
}

function statementListForContainer(container) {
  if (ts.isSourceFile(container) || ts.isBlock(container) || ts.isCaseClause(container) || ts.isDefaultClause(container)) {
    return Array.from(container.statements ?? []);
  }
  return [];
}

function scopeContainersForUse(node, sourceFile) {
  const containers = [];
  let current = node.parent;
  while (current && current !== sourceFile) {
    if ((ts.isBlock(current) || ts.isSourceFile(current) || ts.isCaseClause(current) || ts.isDefaultClause(current)) && !containers.includes(current)) {
      containers.push(current);
    }
    if (isFunctionBoundary(current)) {
      break;
    }
    current = current.parent;
  }
  if (!containers.includes(sourceFile)) containers.push(sourceFile);
  return containers;
}

function parameterShadowsUse(node, targetName, sourceFile) {
  let current = node.parent;
  while (current && current !== sourceFile) {
    if (isFunctionBoundary(current) && parametersDeclareName(current, targetName)) {
      return true;
    }
    if (isFunctionBoundary(current)) {
      break;
    }
    current = current.parent;
  }
  return false;
}

function loopInitializerShadowsUse(node, targetName, sourceFile) {
  let current = node.parent;
  while (current && current !== sourceFile) {
    if (ts.isForStatement(current) && forInitializerDeclaresName(current.initializer, targetName)) return true;
    if ((ts.isForOfStatement(current) || ts.isForInStatement(current)) && forInOfInitializerDeclaresName(current.initializer, targetName)) return true;
    if (isFunctionBoundary(current)) {
      break;
    }
    current = current.parent;
  }
  return false;
}

function catchClauseShadowsUse(node, targetName, sourceFile) {
  let current = node.parent;
  while (current && current !== sourceFile) {
    if (ts.isCatchClause(current) && current.variableDeclaration && declaresNameInBindingName(current.variableDeclaration.name, targetName)) {
      return true;
    }
    if (isFunctionBoundary(current)) {
      break;
    }
    current = current.parent;
  }
  return false;
}

function statementDeclaresAnyName(statement, targetName) {
  const variableDeclaration = directVariableDeclaration(statement, targetName);
  return Boolean(variableDeclaration) || nonVariableStatementDeclaresName(statement, targetName);
}

function switchCaseBlockShadowsUse(node, targetName, sourceFile) {
  let current = node.parent;
  let activeClause = null;
  while (current && current !== sourceFile) {
    if (ts.isCaseClause(current) || ts.isDefaultClause(current)) activeClause = current;
    if (ts.isCaseBlock(current)) {
      for (const clause of current.clauses) {
        if (clause === activeClause) continue;
        for (const statement of clause.statements ?? []) {
          if (statementDeclaresAnyName(statement, targetName)) return true;
        }
      }
      return false;
    }
    if (isFunctionBoundary(current)) break;
    current = current.parent;
  }
  return false;
}

function nodeIsInsideFunction(node, sourceFile) {
  let current = node.parent;
  while (current && current !== sourceFile) {
    if (isFunctionBoundary(current)) {
      return true;
    }
    current = current.parent;
  }
  return false;
}

function assignmentTargetMutatesIdentifier(node, targetName) {
  if (ts.isIdentifier(node)) return node.text === targetName;
  if (ts.isParenthesizedExpression(node)) return assignmentTargetMutatesIdentifier(node.expression, targetName);
  if (ts.isObjectLiteralExpression(node)) {
    return node.properties.some((property) => {
      if (ts.isShorthandPropertyAssignment(property)) return property.name.text === targetName;
      if (ts.isPropertyAssignment(property)) return assignmentTargetMutatesIdentifier(property.initializer, targetName);
      return false;
    });
  }
  if (ts.isArrayLiteralExpression(node)) {
    return node.elements.some((element) => assignmentTargetMutatesIdentifier(element, targetName));
  }
  return false;
}

function expressionMutatesIdentifier(expression, targetName) {
  if (ts.isBinaryExpression(expression) && ASSIGNMENT_OPERATORS.has(expression.operatorToken.kind)) {
    return assignmentTargetMutatesIdentifier(expression.left, targetName);
  }
  if (
    (ts.isPrefixUnaryExpression(expression) || ts.isPostfixUnaryExpression(expression)) &&
    (expression.operator === ts.SyntaxKind.PlusPlusToken || expression.operator === ts.SyntaxKind.MinusMinusToken) &&
    ts.isIdentifier(expression.operand)
  ) {
    return expression.operand.text === targetName;
  }
  return false;
}

function statementMutatesIdentifier(statement, targetName) {
  let mutated = false;
  function visit(node) {
    if (mutated) return;
    if (node !== statement && isFunctionBoundary(node)) return;
    if (expressionMutatesIdentifier(node, targetName)) {
      mutated = true;
      return;
    }
    node.forEachChild(visit);
  }
  visit(statement);
  return mutated;
}

function directVariableDeclaration(statement, targetName) {
  if (!ts.isVariableStatement(statement)) return null;
  if (nodeHasDeclareModifier(statement)) return null;
  let found = null;
  for (const declaration of statement.declarationList.declarations) {
    if (!declaresNameInBindingName(declaration.name, targetName)) continue;
    if (found != null) return { ambiguous: true, declaration: null };
    found = declaration;
  }
  return found == null ? null : { ambiguous: false, declaration: found };
}

function nonVariableStatementDeclaresName(statement, targetName) {
  if (nodeHasDeclareModifier(statement)) return false;
  return (
    ((ts.isFunctionDeclaration(statement) ||
      ts.isClassDeclaration(statement) ||
      ts.isEnumDeclaration(statement) ||
      ts.isModuleDeclaration(statement)) &&
      statement.name?.text === targetName) ||
    (ts.isImportEqualsDeclaration(statement) && !statement.isTypeOnly && statement.name.text === targetName)
  );
}

function statementDeclaredNames(statement) {
  const names = [];
  if (nodeHasDeclareModifier(statement)) return names;
  if (ts.isVariableStatement(statement)) {
    for (const declaration of statement.declarationList.declarations) {
      names.push(...bindingNames(declaration.name));
    }
  }
  if (
    (ts.isFunctionDeclaration(statement) ||
      ts.isClassDeclaration(statement) ||
      ts.isEnumDeclaration(statement) ||
      ts.isModuleDeclaration(statement)) &&
    statement.name
  ) {
    names.push(statement.name.text);
  }
  if (ts.isImportEqualsDeclaration(statement) && !statement.isTypeOnly) names.push(statement.name.text);
  return names;
}

function containerHasPriorLiteralDeclaration(container, targetName, sourceFile, useStart) {
  for (const statement of statementListForContainer(container)) {
    if (statement.getStart(sourceFile) >= useStart) continue;
    const declarationMatch = directVariableDeclaration(statement, targetName);
    if (!declarationMatch || declarationMatch.ambiguous || !declarationMatch.declaration) continue;
    if (!ts.isIdentifier(declarationMatch.declaration.name) || declarationMatch.declaration.initializer == null) continue;
    if (stringLiteralValue(declarationMatch.declaration.initializer) != null) return true;
  }
  return false;
}

function hasOuterResolvableDeclaration(containers, currentIndex, targetName, sourceFile, useStart, bindings) {
  if (bindings.has(targetName)) return true;
  for (let index = currentIndex + 1; index < containers.length; index += 1) {
    if (containerHasPriorLiteralDeclaration(containers[index], targetName, sourceFile, useStart)) return true;
  }
  return false;
}

function resolveIdentifierInContainer(
  targetName,
  container,
  containerIndex,
  containers,
  sourceFile,
  bindings,
  useStart,
  resolvingNames,
  options = {}
) {
  let declaration = null;
  let sawFutureDeclaration = false;
  let sawDynamicDeclaration = false;
  for (const statement of statementListForContainer(container)) {
    const statementStart = statement.getStart(sourceFile);
    const declarationMatch = directVariableDeclaration(statement, targetName);
    const declarationStart = declarationMatch?.declaration?.getStart(sourceFile) ?? null;
    if (declarationMatch && (statementStart >= useStart || (declarationStart != null && declarationStart >= useStart))) {
      sawFutureDeclaration = true;
      continue;
    }
    if (statementStart >= useStart) continue;
    if (declarationMatch) {
      if (declarationMatch.ambiguous || declaration != null) {
        return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName, reason: "target_shadowed_binding" } };
      }
      declaration = declarationMatch.declaration;
      if (!ts.isIdentifier(declaration.name) || declaration.initializer == null) sawDynamicDeclaration = true;
      if (statementMutatesIdentifier(statement, targetName)) {
        return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName, reason: "target_reassigned_binding" } };
      }
      continue;
    }
    if (nonVariableStatementDeclaresName(statement, targetName)) {
      return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName, reason: "target_shadowed_binding" } };
    }
    if (statementMutatesIdentifier(statement, targetName)) {
      return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName, reason: "target_reassigned_binding" } };
    }
  }
  if (declaration != null && ts.isIdentifier(declaration.name) && declaration.initializer != null) {
    if (resolvingNames.has(targetName)) return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName } };
    resolvingNames.add(targetName);
    const resolved = resolveEndpointExpression(declaration.initializer, sourceFile, bindings, resolvingNames, null, options);
    resolvingNames.delete(targetName);
    if (resolved.kind === "resolved" || resolved.kind === "env") {
      const result = expressionResult(resolved.kind, resolved.value, targetName, "local_var");
      if (Array.isArray(resolved.env_names)) result.env_names = uniqueStrings(resolved.env_names);
      if (Array.isArray(resolved.route_params)) result.route_params = resolved.route_params;
      return { status: "resolved", result };
    }
    const reason =
      resolved.reason ??
      (hasOuterResolvableDeclaration(containers, containerIndex, targetName, sourceFile, useStart, bindings)
        ? "target_shadowed_binding"
        : null);
    return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName, reason } };
  }
  if (sawDynamicDeclaration || sawFutureDeclaration) {
    return { status: "failed", result: { kind: "unresolved", value: null, raw: targetName, reason: "target_shadowed_binding" } };
  }
  return { status: "none", result: null };
}

function resolveIdentifierAtUse(node, sourceFile, bindings, resolvingNames, options = {}) {
  const targetName = node.text;
  const useStart = node.getStart(sourceFile);
  if (nodeHasWithAncestor(node, sourceFile)) {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "with_block_present" };
  }
  const containers = scopeContainersForUse(node, sourceFile);
  const insideFunction = nodeIsInsideFunction(node, sourceFile);
  const hasLexicalShadow =
    parameterShadowsUse(node, targetName, sourceFile) ||
    loopInitializerShadowsUse(node, targetName, sourceFile) ||
    catchClauseShadowsUse(node, targetName, sourceFile) ||
    switchCaseBlockShadowsUse(node, targetName, sourceFile);
  for (let index = 0; index < containers.length; index += 1) {
    if (ts.isSourceFile(containers[index])) {
      if (insideFunction) continue;
      if (hasLexicalShadow) {
        return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_shadowed_binding" };
      }
    }
    const resolved = resolveIdentifierInContainer(
      targetName,
      containers[index],
      index,
      containers,
      sourceFile,
      bindings,
      useStart,
      resolvingNames,
      options
    );
    if (resolved.status !== "none") return resolved.result;
  }
  if (hasLexicalShadow) {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_shadowed_binding" };
  }
  if (sourceFileImportDeclaresName(sourceFile, targetName)) {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_shadowed_binding" };
  }
  if (bindings.has(targetName)) return expressionResult("resolved", bindings.get(targetName), targetName, "module_var");
  return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile) };
}

function copyExpressionWithRaw(expression, raw) {
  return { ...expression, raw };
}

function isUrlIdentifierShadowed(identifier, sourceFile) {
  return (
    identifierHasScopedDeclaration(identifier, "URL", sourceFile) ||
    sourceFileImportDeclaresName(sourceFile, "URL") ||
    sourceFileDeclaresName(sourceFile, "URL")
  );
}

function statementListDeclaresName(statements, targetName) {
  return Array.from(statements ?? []).some((statement) => statementDeclaresAnyName(statement, targetName));
}

function identifierHasScopedDeclaration(identifier, targetName, sourceFile) {
  if (identifierHasLocalScopedDeclaration(identifier, targetName, sourceFile)) return true;
  return hoistedVarDeclaresName(sourceFile, targetName);
}

function identifierHasLocalScopedDeclaration(identifier, targetName, sourceFile) {
  let current = identifier.parent;
  while (current && current !== sourceFile) {
    if (isFunctionBoundary(current) && parametersDeclareName(current, targetName)) return true;
    if (isFunctionBoundary(current) && hoistedVarDeclaresName(current, targetName)) return true;
    if (
      (ts.isBlock(current) || ts.isCaseClause(current) || ts.isDefaultClause(current) || ts.isModuleBlock(current)) &&
      statementListDeclaresName(current.statements, targetName)
    ) {
      return true;
    }
    if (ts.isModuleBlock(current) && hoistedVarDeclaresName(current, targetName)) return true;
    if (ts.isForStatement(current) && forInitializerDeclaresName(current.initializer, targetName)) return true;
    if (
      (ts.isForOfStatement(current) || ts.isForInStatement(current)) &&
      forInOfInitializerDeclaresName(current.initializer, targetName)
    ) {
      return true;
    }
    if (
      ts.isCatchClause(current) &&
      current.variableDeclaration &&
      declaresNameInBindingName(current.variableDeclaration.name, targetName)
    ) {
      return true;
    }
    current = current.parent;
  }
  return false;
}

function envPlaceholderFromAccess(node) {
  function isProcessEnv(expr) {
    return (
      ts.isPropertyAccessExpression(expr) &&
      expr.name.text === "env" &&
      ts.isIdentifier(expr.expression) &&
      expr.expression.text === "process"
    );
  }
  function isImportMetaEnv(expr) {
    return (
      ts.isPropertyAccessExpression(expr) &&
      expr.name.text === "env" &&
      ts.isMetaProperty(expr.expression) &&
      expr.expression.keywordToken === ts.SyntaxKind.ImportKeyword &&
      expr.expression.name.text === "meta"
    );
  }
  if (ts.isPropertyAccessExpression(node) && (isProcessEnv(node.expression) || isImportMetaEnv(node.expression))) {
    return { placeholder: `\${env:${node.name.text}}`, name: node.name.text };
  }
  if (
    ts.isElementAccessExpression(node) &&
    (isProcessEnv(node.expression) || isImportMetaEnv(node.expression)) &&
    ts.isStringLiteral(node.argumentExpression)
  ) {
    return { placeholder: `\${env:${node.argumentExpression.text}}`, name: node.argumentExpression.text };
  }
  return null;
}

function expressionEnvNames(expression) {
  return Array.isArray(expression.env_names) ? expression.env_names : [];
}

function decodeRouteParamBraces(pathname) {
  return pathname.replace(/%7B/gi, "{").replace(/%7D/gi, "}");
}

function isAbsoluteUrlLike(value) {
  return value.startsWith("//") || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(value);
}

function envHostPlaceholderPrefix(value) {
  const text = String(value);
  const match = /^\$\{env:[^}]+\}/.exec(text);
  if (!match) return null;
  const suffix = text.slice(match[0].length);
  if (suffix !== "" && !suffix.startsWith("/")) return null;
  return match ? match[0] : null;
}

function resolveUrlConstructorToString(node, sourceFile, bindings, resolvingNames, parameterBindings, options = {}) {
  if (
    !ts.isCallExpression(node) ||
    node.arguments.length !== 0 ||
    !ts.isPropertyAccessExpression(node.expression) ||
    node.expression.name.text !== "toString" ||
    !ts.isNewExpression(node.expression.expression)
  ) {
    return null;
  }
  const newExpression = node.expression.expression;
  if (
    !ts.isIdentifier(newExpression.expression) ||
    newExpression.expression.text !== "URL" ||
    isUrlIdentifierShadowed(newExpression.expression, sourceFile)
  ) {
    return null;
  }
  const args = Array.from(newExpression.arguments ?? []);
  if (args.length !== 2) return null;
  const pathExpression = resolveEndpointExpression(args[0], sourceFile, bindings, resolvingNames, parameterBindings, options);
  const baseExpression = resolveEndpointExpression(args[1], sourceFile, bindings, resolvingNames, parameterBindings, options);
  if (pathExpression.kind !== "resolved" || baseExpression.value == null) return null;
  if (baseExpression.kind !== "resolved" && baseExpression.kind !== "env") return null;
  const pathValue = String(pathExpression.value).trim();
  if (isAbsoluteUrlLike(pathValue)) return null;
  try {
    if (baseExpression.kind === "env") {
      if (!pathValue.startsWith("/")) return null;
      const pathname = decodeRouteParamBraces(new URL(pathValue, "https://example.invalid").pathname || "/");
      const envPrefix = envHostPlaceholderPrefix(baseExpression.value);
      if (envPrefix == null) return null;
      const result = expressionResult("env", `${envPrefix}${pathname}`, rawNodeText(node, sourceFile), "url_constructor");
      const envNames = expressionEnvNames(baseExpression);
      if (envNames.length > 0) result.env_names = uniqueStrings(envNames);
      if (Array.isArray(pathExpression.route_params)) result.route_params = pathExpression.route_params;
      return result;
    }
    const pathname = decodeRouteParamBraces(new URL(pathValue, String(baseExpression.value)).pathname || "/");
    const result = expressionResult("resolved", pathname, rawNodeText(node, sourceFile), "url_constructor");
    if (Array.isArray(pathExpression.route_params)) result.route_params = pathExpression.route_params;
    return result;
  } catch {
    return null;
  }
}

function helperBodyReturnExpression(functionNode) {
  if (ts.isArrowFunction(functionNode) && !ts.isBlock(functionNode.body)) return functionNode.body;
  const body = functionNode.body;
  if (!body || !ts.isBlock(body) || body.statements.length !== 1) return null;
  const statement = body.statements[0];
  return ts.isReturnStatement(statement) && statement.expression ? statement.expression : null;
}

function helperParameterNames(functionNode) {
  const names = [];
  for (const parameter of functionNode.parameters ?? []) {
    if (parameter.dotDotDotToken) return null;
    if (!ts.isIdentifier(parameter.name)) return null;
    names.push(parameter.name.text);
  }
  return names;
}

function topLevelHelperCandidates(sourceFile, helperName, useNode) {
  const useStart = useNode.getStart(sourceFile);
  const candidates = [];
  for (const statement of sourceFile.statements) {
    if (statement.getStart(sourceFile) >= useStart) break;
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === helperName && statement.body) {
      candidates.push({ functionNode: statement, statement });
      continue;
    }
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name) || declaration.name.text !== helperName) continue;
      const initializer = declaration.initializer;
      if (initializer && (ts.isArrowFunction(initializer) || ts.isFunctionExpression(initializer))) {
        candidates.push({ functionNode: initializer, statement });
      }
    }
  }
  return candidates;
}

function helperIsReassigned(sourceFile, helperName, declarationStatement) {
  for (const statement of sourceFile.statements) {
    if (statement === declarationStatement) continue;
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === helperName && !statement.body) continue;
    if (
      statementDeclaresAnyName(statement, helperName) ||
      (!isHoistedVarBoundary(statement) && hoistedVarDeclaresName(statement, helperName)) ||
      statementMutatesIdentifier(statement, helperName)
    ) {
      return true;
    }
  }
  return false;
}

function resolveDirectReturnHelperCall(node, sourceFile, bindings, resolvingNames, parameterBindings, options = {}) {
  if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression)) return null;
  const helperName = node.expression.text;
  const helperKey = `helper:${helperName}`;
  if (resolvingNames.has(helperKey)) {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_helper_call_deferred" };
  }
  if (identifierHasLocalScopedDeclaration(node.expression, helperName, sourceFile)) return null;
  if (sourceFileImportDeclaresName(sourceFile, helperName)) return null;
  const candidates = topLevelHelperCandidates(sourceFile, helperName, node.expression);
  if (candidates.length === 0) return null;
  if (candidates.length > 1 || helperIsReassigned(sourceFile, helperName, candidates[0].statement)) {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_helper_reassigned" };
  }
  const helper = candidates[0].functionNode;
  const returnExpression = helperBodyReturnExpression(helper);
  const parameterNames = helperParameterNames(helper);
  if (returnExpression == null || parameterNames == null || node.arguments.length < parameterNames.length) {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_helper_call_deferred" };
  }
  const nextParameterBindings = new Map(parameterBindings ?? []);
  for (let index = 0; index < parameterNames.length; index += 1) {
    const resolved = resolveEndpointExpression(node.arguments[index], sourceFile, bindings, resolvingNames, parameterBindings, options);
    if (resolved.kind !== "resolved" && resolved.kind !== "env") {
      return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_helper_call_deferred" };
    }
    nextParameterBindings.set(parameterNames[index], resolved);
  }
  const nextResolving = new Set(resolvingNames);
  nextResolving.add(helperKey);
  const resolved = resolveEndpointExpression(returnExpression, sourceFile, bindings, nextResolving, nextParameterBindings, options);
  if (resolved.kind !== "resolved" && resolved.kind !== "env") {
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_helper_call_deferred" };
  }
  const result = copyExpressionWithRaw(resolved, rawNodeText(node, sourceFile));
  result.resolution_kind = "helper_inline";
  return result;
}

function resolveEndpointExpression(node, sourceFile, bindings, resolvingNames = new Set(), parameterBindings = null, options = {}) {
  const literal = stringLiteralValue(node);
  if (literal != null) return expressionResult("resolved", literal, literal, "literal");
  const env = envPlaceholderFromAccess(node);
  if (env != null) return { kind: "env", value: env.placeholder, raw: rawNodeText(node, sourceFile), env_names: [env.name] };
  if (node.kind === ts.SyntaxKind.ParenthesizedExpression && node.expression) {
    return copyExpressionWithRaw(
      resolveEndpointExpression(node.expression, sourceFile, bindings, resolvingNames, parameterBindings, options),
      rawNodeText(node, sourceFile)
    );
  }
  if (ts.isIdentifier(node)) {
    if (parameterBindings?.has(node.text)) return copyExpressionWithRaw(parameterBindings.get(node.text), node.text);
    return resolveIdentifierAtUse(node, sourceFile, bindings, resolvingNames, options);
  }
  if (ts.isTemplateExpression(node)) {
    let value = node.head.text;
    let hostUnresolved = false;
    let unresolvedHostRaw = null;
    const routeParams = [];
    let envNames = [];
    for (let index = 0; index < node.templateSpans.length; index += 1) {
      const span = node.templateSpans[index];
      const resolved = resolveEndpointExpression(span.expression, sourceFile, bindings, resolvingNames, parameterBindings, options);
      if (resolved.kind === "env") {
        value += resolved.value;
        hostUnresolved = true;
        if (Array.isArray(resolved.env_names)) envNames = envNames.concat(resolved.env_names);
      } else if (resolved.kind === "resolved") {
        value += resolved.value;
      } else {
        const leadingRelativePathParam = canTreatTemplateSpanAsLeadingRelativePathParam(
          value,
          span.literal.text,
          index,
          span.expression,
          node,
          sourceFile,
          options
        );
        if (leadingRelativePathParam) {
          value = "/";
        }
        if (!leadingRelativePathParam && canTreatTemplateSpanAsUnresolvedHost(value, span.literal.text, index, span.expression)) {
          hostUnresolved = true;
          unresolvedHostRaw = rawNodeText(span.expression, sourceFile);
        } else {
          const paramName = routeParamNameFromExpression(span.expression);
          if (paramName == null || !isSafeRouteParamName(paramName)) {
            return {
              kind: "unresolved",
              value: null,
              raw: rawNodeText(node, sourceFile),
              reason: "template_dynamic_expression_unsafe",
            };
          }
          const reason = templateParameterizationFailure(value, span.literal.text, index < node.templateSpans.length - 1, options);
          if (reason != null) {
            return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason };
          }
          value += `{${paramName}}`;
          routeParams.push(paramName);
        }
      }
      value += span.literal.text;
    }
    const result = expressionResult(
      unresolvedHostRaw != null ? "host_unresolved" : hostUnresolved ? "env" : "resolved",
      value,
      rawNodeText(node, sourceFile),
      routeParams.length > 0 ? "template_parameterized" : "template"
    );
    if (routeParams.length > 0) result.route_params = uniqueStrings(routeParams);
    if (envNames.length > 0) result.env_names = uniqueStrings(envNames);
    if (unresolvedHostRaw != null) {
      result.reason = "template_dynamic_host_position";
      result.host_raw = unresolvedHostRaw;
      result.host_resolution_kind = "expression_unresolved";
    }
    return result;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = resolveEndpointExpression(node.left, sourceFile, bindings, resolvingNames, parameterBindings, options);
    const right = resolveEndpointExpression(node.right, sourceFile, bindings, resolvingNames, parameterBindings, options);
    if ((left.kind === "resolved" || left.kind === "env") && (right.kind === "resolved" || right.kind === "env")) {
      const result = expressionResult(
        left.kind === "env" || right.kind === "env" ? "env" : "resolved",
        `${left.value}${right.value}`,
        rawNodeText(node, sourceFile),
        "concat"
      );
      const envNames = [
        ...(Array.isArray(left.env_names) ? left.env_names : []),
        ...(Array.isArray(right.env_names) ? right.env_names : []),
      ];
      if (envNames.length > 0) result.env_names = uniqueStrings(envNames);
      const routeParams = [
        ...(Array.isArray(left.route_params) ? left.route_params : []),
        ...(Array.isArray(right.route_params) ? right.route_params : []),
      ];
      if (routeParams.length > 0) result.route_params = uniqueStrings(routeParams);
      return result;
    }
  }
  if (ts.isCallExpression(node)) {
    const urlResolved = resolveUrlConstructorToString(node, sourceFile, bindings, resolvingNames, parameterBindings, options);
    if (urlResolved != null) return urlResolved;
    const helperResolved = resolveDirectReturnHelperCall(node, sourceFile, bindings, resolvingNames, parameterBindings, options);
    if (helperResolved != null) return helperResolved;
    return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile), reason: "target_helper_call_deferred" };
  }
  return { kind: "unresolved", value: null, raw: rawNodeText(node, sourceFile) };
}

function splitResolvedEndpointTarget(value) {
  const trimmed = value.trim();
  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    try {
      const parsed = new URL(trimmed);
      const host = parsed.hostname || null;
      const external = host != null && host !== "localhost" && host !== "127.0.0.1";
      return { kind: external ? "external" : "resolved", path: parsed.pathname || "/", host, raw_target: trimmed };
    } catch {
      return { kind: "unresolved", path: null, host: null, raw_target: trimmed };
    }
  }
  if (trimmed.startsWith("${env:")) {
    const hostEnd = trimmed.indexOf("}");
    if (hostEnd >= 0) {
      const pathStart = trimmed.indexOf("/", hostEnd + 1);
      if (pathStart === hostEnd + 1 && !trimmed.slice(pathStart).includes("${env:")) {
        return {
          kind: "host_unresolved",
          path: trimmed.slice(pathStart) || "/",
          host: trimmed.slice(0, hostEnd + 1),
          raw_target: trimmed,
          reason: "host_env_backed",
          host_resolution_kind: "env_backed_unresolved",
        };
      }
    }
    return { kind: "unresolved", path: null, host: null, raw_target: trimmed };
  }
  if (!trimmed.startsWith("/")) return { kind: "unresolved", path: null, host: null, raw_target: trimmed };
  return { kind: "resolved", path: trimmed, host: null, raw_target: trimmed };
}

function resolveEndpointTarget(node, sourceFile, bindings, options = {}) {
  const expression = resolveEndpointExpression(node, sourceFile, bindings, new Set(), null, options);
  if (expression.kind === "unresolved" || expression.value == null) {
    return { kind: "unresolved", path: null, host: null, raw_target: expression.raw, reason: expression.reason ?? null };
  }
  if (expression.kind === "host_unresolved") {
    const target = splitResolvedEndpointTarget(normalizeConfiguredPath(expression.value));
    if (target.kind === "resolved") {
      target.kind = "host_unresolved";
      target.host = null;
      target.raw_target = expression.raw;
      target.reason = expression.reason ?? "template_dynamic_host_position";
      target.host_resolution_kind = expression.host_resolution_kind ?? "expression_unresolved";
      return carryEndpointExpressionDetails(target, expression);
    }
    return { kind: "unresolved", path: null, host: null, raw_target: expression.raw, reason: expression.reason ?? null };
  }
  const target = splitResolvedEndpointTarget(expression.value);
  target.resolution_kind = expression.resolution_kind ?? null;
  if (Array.isArray(expression.route_params)) target.route_params = expression.route_params;
  if (target.kind === "host_unresolved" && Array.isArray(expression.env_names)) target.env_names = expression.env_names;
  if (target.kind === "host_unresolved" || target.kind === "resolved" || target.kind === "external") return target;
  return { kind: "unresolved", path: null, host: null, raw_target: expression.raw };
}

function collectExpressFactories(sourceFile) {
  const expressFactories = new Set();
  const routerFactories = new Set();
  for (const statement of sourceFile.statements) {
    if (ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier) && statement.moduleSpecifier.text === "express") {
      const clause = statement.importClause;
      if (clause?.name) expressFactories.add(clause.name.text);
      if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) expressFactories.add(clause.namedBindings.name.text);
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const element of clause.namedBindings.elements) {
          if ((element.propertyName ?? element.name).text === "Router") routerFactories.add(element.name.text);
        }
      }
      continue;
    }

    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        const init = declaration.initializer;
        if (
          init &&
          ts.isCallExpression(init) &&
          ts.isIdentifier(init.expression) &&
          init.expression.text === "require" &&
          init.arguments.length === 1 &&
          ts.isStringLiteral(init.arguments[0]) &&
          init.arguments[0].text === "express" &&
          ts.isIdentifier(declaration.name)
        ) {
          expressFactories.add(declaration.name.text);
        }
      }
    }
  }
  return { expressFactories, routerFactories };
}

function isRequireCall(node, moduleName) {
  return (
    ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === "require" &&
    node.arguments.length === 1 &&
    ts.isStringLiteral(node.arguments[0]) &&
    node.arguments[0].text === moduleName
  );
}

function requireCallModule(node) {
  if (
    ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === "require" &&
    node.arguments.length === 1 &&
    ts.isStringLiteral(node.arguments[0])
  ) {
    return node.arguments[0].text;
  }
  return null;
}

function isExpressFactoryCall(node, expressFactories) {
  return ts.isCallExpression(node) && ts.isIdentifier(node.expression) && expressFactories.has(node.expression.text);
}

function isInlineExpressFactoryCall(node) {
  return ts.isCallExpression(node) && isRequireCall(node.expression, "express");
}

function isExpressRouterFactoryCall(node, expressFactories, routerFactories) {
  if (!ts.isCallExpression(node)) return false;
  if (ts.isIdentifier(node.expression) && routerFactories.has(node.expression.text)) return true;
  if (!ts.isPropertyAccessExpression(node.expression)) return false;
  return (
    node.expression.name.text === "Router" &&
    ts.isIdentifier(node.expression.expression) &&
    expressFactories.has(node.expression.expression.text)
  );
}

function collectExpressReceivers(sourceFile) {
  const { expressFactories, routerFactories } = collectExpressFactories(sourceFile);
  const receivers = new Set();
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
      if (
        isExpressFactoryCall(node.initializer, expressFactories) ||
        isInlineExpressFactoryCall(node.initializer) ||
        isExpressRouterFactoryCall(node.initializer, expressFactories, routerFactories)
      ) {
        receivers.add(node.name.text);
      }
    }
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
      ts.isIdentifier(node.left) &&
      (isExpressFactoryCall(node.right, expressFactories) ||
        isInlineExpressFactoryCall(node.right) ||
        isExpressRouterFactoryCall(node.right, expressFactories, routerFactories))
    ) {
      receivers.add(node.left.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return receivers;
}

function directExpressRoute(node, sourceFile, receivers) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return null;
  const method = node.expression.name.text;
  if (!EXPRESS_ROUTE_METHODS.has(method)) return null;
  if (!ts.isIdentifier(node.expression.expression) || !receivers.has(node.expression.expression.text)) return null;
  if (node.arguments.length < 1) return null;
  const routePath = stringLiteralValue(node.arguments[0]);
  if (routePath == null) return null;
  return { method, path: routePath, line: lineOf(sourceFile, node.expression.getStart(sourceFile)), source_kind: `express_${method}` };
}

function chainedExpressRoute(node, sourceFile, receivers) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return null;
  const method = node.expression.name.text;
  if (!EXPRESS_ROUTE_METHODS.has(method)) return null;
  const innerCall = node.expression.expression;
  if (!ts.isCallExpression(innerCall) || !ts.isPropertyAccessExpression(innerCall.expression)) return null;
  if (innerCall.expression.name.text !== "route") return null;
  if (!ts.isIdentifier(innerCall.expression.expression) || !receivers.has(innerCall.expression.expression.text)) return null;
  if (innerCall.arguments.length < 1) return null;
  const routePath = stringLiteralValue(innerCall.arguments[0]);
  if (routePath == null) return null;
  return { method, path: routePath, line: lineOf(sourceFile, node.expression.getStart(sourceFile)), source_kind: `express_${method}` };
}

function collectExpressRoutes(sourceFile) {
  const receivers = collectExpressReceivers(sourceFile);
  if (receivers.size === 0) return [];
  const routes = [];
  function visit(node) {
    const route = directExpressRoute(node, sourceFile, receivers) ?? chainedExpressRoute(node, sourceFile, receivers);
    if (route) routes.push(route);
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return routes;
}

function collectFastifyFactories(sourceFile) {
  const factories = new Set();
  for (const statement of sourceFile.statements) {
    if (ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier) && statement.moduleSpecifier.text === "fastify") {
      const clause = statement.importClause;
      if (clause?.name) factories.add(clause.name.text);
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const element of clause.namedBindings.elements) {
          if ((element.propertyName ?? element.name).text === "fastify") factories.add(element.name.text);
        }
      }
      continue;
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name) && declaration.initializer && isRequireCall(declaration.initializer, "fastify")) {
          factories.add(declaration.name.text);
        }
      }
    }
  }
  return factories;
}

function collectFastifyReceivers(sourceFile) {
  const factories = collectFastifyFactories(sourceFile);
  const receivers = new Set();
  function visit(node) {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      ts.isCallExpression(node.initializer) &&
      ts.isIdentifier(node.initializer.expression) &&
      factories.has(node.initializer.expression.text)
    ) {
      receivers.add(node.name.text);
    }
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      ts.isCallExpression(node.initializer) &&
      isRequireCall(node.initializer.expression, "fastify")
    ) {
      receivers.add(node.name.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return receivers;
}

function directFastifyRoute(node, sourceFile, receivers) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return null;
  const method = node.expression.name.text;
  if (!EXPRESS_ROUTE_METHODS.has(method)) return null;
  if (!ts.isIdentifier(node.expression.expression) || !receivers.has(node.expression.expression.text)) return null;
  if (node.arguments.length < 1) return null;
  const routePath = stringLiteralValue(node.arguments[0]);
  if (routePath == null) return null;
  return { method, path: routePath, line: lineOf(sourceFile, node.expression.getStart(sourceFile)), source_kind: `fastify_${method}` };
}

function fastifyRouteObject(node, sourceFile, receivers) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return null;
  if (node.expression.name.text !== "route") return null;
  if (!ts.isIdentifier(node.expression.expression) || !receivers.has(node.expression.expression.text)) return null;
  if (node.arguments.length < 1 || !ts.isObjectLiteralExpression(node.arguments[0])) return null;
  const routeObject = node.arguments[0];
  if (objectLiteralHasDynamicProperty(routeObject)) return null;
  const pathNode = objectLiteralProperty(routeObject, "url") ?? objectLiteralProperty(routeObject, "path");
  const hasMethodProperty = objectLiteralHasProperty(routeObject, "method");
  const methodNode = objectLiteralProperty(routeObject, "method");
  const routePath = pathNode ? stringLiteralValue(pathNode) : null;
  if (routePath == null) return null;
  const method = methodNode ? stringLiteralValue(methodNode) : null;
  if (hasMethodProperty && method == null) return null;
  return {
    method: method ? method.toLowerCase() : "all",
    path: routePath,
    line: lineOf(sourceFile, node.expression.getStart(sourceFile)),
    source_kind: "fastify_route",
  };
}

function collectFastifyRoutes(sourceFile) {
  const receivers = collectFastifyReceivers(sourceFile);
  if (receivers.size === 0) return [];
  const routes = [];
  function visit(node) {
    const route = directFastifyRoute(node, sourceFile, receivers) ?? fastifyRouteObject(node, sourceFile, receivers);
    if (route) routes.push(route);
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return routes;
}

function collectKoaRouterFactories(sourceFile) {
  const factories = new Set();
  for (const statement of sourceFile.statements) {
    if (
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      (statement.moduleSpecifier.text === "@koa/router" || statement.moduleSpecifier.text === "koa-router")
    ) {
      const clause = statement.importClause;
      if (clause?.name) factories.add(clause.name.text);
      if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) factories.add(clause.namedBindings.name.text);
      if (clause?.namedBindings && ts.isNamedImports(clause.namedBindings)) {
        for (const element of clause.namedBindings.elements) {
          const importedName = (element.propertyName ?? element.name).text;
          if (importedName === "Router" || importedName === "default") factories.add(element.name.text);
        }
      }
      continue;
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue;
        const moduleName = requireCallModule(declaration.initializer);
        if (moduleName === "@koa/router" || moduleName === "koa-router") factories.add(declaration.name.text);
      }
    }
  }
  return factories;
}

function isKoaRouterInstance(node, factories) {
  if (ts.isNewExpression(node) && ts.isIdentifier(node.expression) && factories.has(node.expression.text)) return true;
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && factories.has(node.expression.text)) return true;
  if (ts.isCallExpression(node) && (isRequireCall(node.expression, "@koa/router") || isRequireCall(node.expression, "koa-router"))) return true;
  return false;
}

function collectKoaReceivers(sourceFile) {
  const factories = collectKoaRouterFactories(sourceFile);
  const receivers = new Set();
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer && isKoaRouterInstance(node.initializer, factories)) {
      receivers.add(node.name.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return receivers;
}

function directKoaRoute(node, sourceFile, receivers) {
  if (!ts.isCallExpression(node) || !ts.isPropertyAccessExpression(node.expression)) return null;
  const rawMethod = node.expression.name.text;
  if (!KOA_ROUTE_METHODS.has(rawMethod)) return null;
  const method = rawMethod === "del" ? "delete" : rawMethod;
  if (!ts.isIdentifier(node.expression.expression) || !receivers.has(node.expression.expression.text)) return null;
  if (node.arguments.length < 1) return null;
  const routePath = stringLiteralValue(node.arguments[0]);
  if (routePath == null) return null;
  return { method, path: routePath, line: lineOf(sourceFile, node.expression.getStart(sourceFile)), source_kind: `koa_${method}` };
}

function collectKoaRoutes(sourceFile) {
  const receivers = collectKoaReceivers(sourceFile);
  if (receivers.size === 0) return [];
  const routes = [];
  function visit(node) {
    const route = directKoaRoute(node, sourceFile, receivers);
    if (route) routes.push(route);
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return routes;
}

function collectServerRoutes(sourceFile) {
  return [...collectExpressRoutes(sourceFile), ...collectFastifyRoutes(sourceFile), ...collectKoaRoutes(sourceFile)];
}

function collectAxiosLocals(sourceFile) {
  const locals = new Set();
  for (const statement of sourceFile.statements) {
    if (ts.isImportDeclaration(statement) && ts.isStringLiteral(statement.moduleSpecifier) && statement.moduleSpecifier.text === "axios") {
      const clause = statement.importClause;
      if (clause?.name) locals.add(clause.name.text);
      if (clause?.namedBindings && ts.isNamespaceImport(clause.namedBindings)) locals.add(clause.namedBindings.name.text);
      continue;
    }

    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        const init = declaration.initializer;
        if (
          init &&
          ts.isCallExpression(init) &&
          ts.isIdentifier(init.expression) &&
          init.expression.text === "require" &&
          init.arguments.length === 1 &&
          ts.isStringLiteral(init.arguments[0]) &&
          init.arguments[0].text === "axios" &&
          ts.isIdentifier(declaration.name)
        ) {
          locals.add(declaration.name.text);
        }
      }
    }
  }
  return locals;
}

function propertyNameText(name) {
  if (ts.isIdentifier(name) || ts.isStringLiteral(name)) return name.text;
  return null;
}

function objectLiteralProperty(objectNode, propertyName) {
  if (!ts.isObjectLiteralExpression(objectNode)) return null;
  for (const property of objectNode.properties) {
    if (ts.isPropertyAssignment(property) && propertyNameText(property.name) === propertyName) return property.initializer;
    if (ts.isShorthandPropertyAssignment(property) && propertyNameText(property.name) === propertyName) return property.name;
  }
  return null;
}

function objectLiteralPropertyFromNames(objectNode, propertyNames) {
  if (!ts.isObjectLiteralExpression(objectNode)) return null;
  for (const propertyName of propertyNames) {
    const property = objectLiteralProperty(objectNode, propertyName);
    if (property) return { name: propertyName, initializer: property };
  }
  return null;
}

function objectLiteralHasProperty(objectNode, propertyName) {
  if (!ts.isObjectLiteralExpression(objectNode)) return false;
  for (const property of objectNode.properties) {
    if (ts.isPropertyAssignment(property) || ts.isShorthandPropertyAssignment(property) || ts.isMethodDeclaration(property)) {
      if (propertyNameText(property.name) === propertyName) return true;
    }
  }
  return false;
}

function objectLiteralHasDynamicProperty(objectNode) {
  if (!ts.isObjectLiteralExpression(objectNode)) return false;
  return objectNode.properties.some(
    (property) =>
      ts.isSpreadAssignment(property) ||
      ((ts.isPropertyAssignment(property) || ts.isShorthandPropertyAssignment(property) || ts.isMethodDeclaration(property)) &&
        property.name != null &&
        ts.isComputedPropertyName(property.name))
  );
}

function axiosCreateClientInfo(name, initializer, sourceFile, axiosLocals, bindings) {
  if (
    !initializer ||
    !ts.isCallExpression(initializer) ||
    !ts.isPropertyAccessExpression(initializer.expression) ||
    initializer.expression.name.text !== "create" ||
    !ts.isIdentifier(initializer.expression.expression) ||
    !axiosLocals.has(initializer.expression.expression.text)
  ) {
    return null;
  }
  let baseUrl = null;
  if (initializer.arguments.length >= 1 && ts.isObjectLiteralExpression(initializer.arguments[0])) {
    const baseUrlNode = objectLiteralProperty(initializer.arguments[0], "baseURL");
    if (baseUrlNode) baseUrl = resolveEndpointExpression(baseUrlNode, sourceFile, bindings);
  }
  return {
    local_name: name,
    base_url: baseUrl,
    defining_line: lineOf(sourceFile, initializer.getStart(sourceFile)),
  };
}

function collectAxiosClients(sourceFile, axiosLocals, bindings) {
  const clients = new Set();
  const baseUrls = new Map();
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      const clientInfo = axiosCreateClientInfo(node.name.text, node.initializer, sourceFile, axiosLocals, bindings);
      if (clientInfo) {
        clients.add(node.name.text);
        if (clientInfo.base_url) baseUrls.set(node.name.text, clientInfo.base_url);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return { clients, baseUrls };
}

function statementHasExportModifier(statement) {
  return statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword) ?? false;
}

function collectTopLevelAxiosClientInfos(sourceFile, axiosLocals, bindings) {
  const clients = new Map();
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name)) continue;
      const clientInfo = axiosCreateClientInfo(declaration.name.text, declaration.initializer, sourceFile, axiosLocals, bindings);
      if (clientInfo) clients.set(declaration.name.text, clientInfo);
    }
  }
  return clients;
}

function collectModuleClients(sourceFile, axiosLocals, bindings) {
  const localClients = collectTopLevelAxiosClientInfos(sourceFile, axiosLocals, bindings);
  const moduleClients = { default: null, named: Object.create(null) };

  for (const statement of sourceFile.statements) {
    if (ts.isVariableStatement(statement) && statementHasExportModifier(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name)) continue;
        const clientInfo = localClients.get(declaration.name.text);
        if (clientInfo) moduleClients.named[declaration.name.text] = clientInfo;
      }
      continue;
    }

    if (ts.isExportAssignment(statement) && ts.isIdentifier(statement.expression)) {
      const clientInfo = localClients.get(statement.expression.text);
      if (clientInfo) moduleClients.default = clientInfo;
      continue;
    }
    if (ts.isExportAssignment(statement)) {
      const clientInfo = axiosCreateClientInfo("default", statement.expression, sourceFile, axiosLocals, bindings);
      if (clientInfo) moduleClients.default = clientInfo;
      continue;
    }

    if (ts.isExportDeclaration(statement) && !statement.moduleSpecifier && statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      for (const element of statement.exportClause.elements) {
        const localName = (element.propertyName ?? element.name).text;
        const exportName = element.name.text;
        const clientInfo = localClients.get(localName);
        if (!clientInfo) continue;
        if (exportName === "default") {
          moduleClients.default = clientInfo;
        } else {
          moduleClients.named[exportName] = clientInfo;
        }
      }
    }
  }

  return moduleClients;
}

function methodFromOptionsLike(node) {
  const methodNode = objectLiteralProperty(node, "method");
  if (methodNode) {
    const value = stringLiteralValue(methodNode);
    return value == null ? null : value.toUpperCase();
  }
  const initNode = objectLiteralProperty(node, "init");
  if (initNode && ts.isObjectLiteralExpression(initNode)) return methodFromOptionsLike(initNode);
  return null;
}

function httpWrapperMethodName(name) {
  return HTTP_WRAPPER_METHODS.get(name) ?? null;
}

function endpointConfigTargetProperty(configNode) {
  return objectLiteralPropertyFromNames(configNode, ENDPOINT_CONFIG_TARGET_PROPERTIES);
}

function endpointConfigBaseUrlProperty(configNode) {
  return objectLiteralPropertyFromNames(configNode, ENDPOINT_CONFIG_BASE_URL_PROPERTIES);
}

function endpointConfigHasWrapperContext(configNode) {
  return (
    endpointConfigBaseUrlProperty(configNode) != null ||
    objectLiteralProperty(configNode, "host") != null ||
    objectLiteralProperty(configNode, "service") != null ||
    objectLiteralProperty(configNode, "port") != null ||
    objectLiteralProperty(configNode, "apiVersion") != null ||
    objectLiteralProperty(configNode, "clientAppId") != null
  );
}

function endpointConfigHasBaseContext(configNode) {
  return (
    endpointConfigBaseUrlProperty(configNode) != null ||
    objectLiteralProperty(configNode, "host") != null ||
    objectLiteralProperty(configNode, "service") != null
  );
}

function endpointConfigLooksLikeAxiosRequestConfig(configNode) {
  // Axios uses baseURL; lowercase baseUrl remains available for app-level wrapper configs.
  const targetProperty = endpointConfigTargetProperty(configNode);
  return (
    targetProperty != null &&
    targetProperty.name === "url" &&
    objectLiteralProperty(configNode, "baseURL") != null &&
    objectLiteralProperty(configNode, "service") == null &&
    objectLiteralProperty(configNode, "host") == null &&
    objectLiteralProperty(configNode, "port") == null &&
    objectLiteralProperty(configNode, "apiVersion") == null &&
    objectLiteralProperty(configNode, "clientAppId") == null
  );
}

function composedTargetWithBaseUrl(targetNode, sourceFile, bindings, baseUrlExpression) {
  const target = resolveEndpointExpression(targetNode, sourceFile, bindings, new Set(), null, { relativePathAllowed: true });
  if (!baseUrlExpression) return resolveEndpointTarget(targetNode, sourceFile, bindings);
  if (target.kind === "unresolved" || target.value == null) {
    // Preserve the configured-client resolver's more specific relative-template failure reason.
    return { kind: "unresolved", path: null, host: null, raw_target: target.raw, reason: target.reason ?? null };
  }
  const targetValue = target.value.trim();
  if (targetValue.startsWith("http://") || targetValue.startsWith("https://") || targetValue.startsWith("${env:")) {
    return resolveEndpointTarget(targetNode, sourceFile, bindings);
  }
  if (baseUrlExpression.kind !== "resolved" && baseUrlExpression.kind !== "env") {
    return targetWithUnresolvedHostPathExpression(
      target,
      target.raw,
      "host_or_service_unresolved",
      baseUrlExpression.raw,
      "base_url_raw"
    );
  }
  const baseValue = String(baseUrlExpression.value).trim();
  const combined = `${baseValue.replace(/\/+$/, "")}/${targetValue.replace(/^\/+/, "")}`;
  const resolved = splitResolvedEndpointTarget(combined);
  resolved.resolution_kind = target.resolution_kind ?? null;
  if (Array.isArray(target.route_params)) resolved.route_params = target.route_params;
  const envNames = [
    ...(Array.isArray(baseUrlExpression.env_names) ? baseUrlExpression.env_names : []),
    ...(Array.isArray(target.env_names) ? target.env_names : []),
  ];
  if (resolved.kind === "host_unresolved" && envNames.length > 0) resolved.env_names = uniqueStrings(envNames);
  return resolved.kind === "unresolved" ? { kind: "unresolved", path: null, host: null, raw_target: target.raw } : resolved;
}

function normalizeConfiguredPath(value) {
  const trimmed = String(value).trim();
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}

function trimLeadingSlashes(value) {
  let start = 0;
  while (start < value.length && value[start] === "/") start += 1;
  return value.slice(start);
}

function trimTrailingSlashes(value) {
  let end = value.length;
  while (end > 0 && value[end - 1] === "/") end -= 1;
  return value.slice(0, end);
}

function carryEndpointExpressionDetails(resolved, expression) {
  resolved.resolution_kind = expression.resolution_kind ?? null;
  if (Array.isArray(expression.route_params)) resolved.route_params = expression.route_params;
  if (resolved.kind === "host_unresolved" && Array.isArray(expression.env_names)) resolved.env_names = expression.env_names;
  for (const key of ["host_raw", "service_raw", "base_url_raw"]) {
    if (typeof expression[key] === "string") resolved[key] = expression[key];
  }
  return resolved;
}

function targetWithUnresolvedHostPathExpression(target, rawTarget, reason, unresolvedBaseRaw = null, unresolvedBaseRawKey = "host_raw") {
  if (target.kind === "unresolved" || target.value == null) {
    return { kind: "unresolved", path: null, host: null, raw_target: target.raw, reason: target.reason ?? null };
  }
  const targetValue = String(target.value).trim();
  if (targetValue.startsWith("http://") || targetValue.startsWith("https://") || targetValue.startsWith("${env:")) {
    const resolved = splitResolvedEndpointTarget(targetValue);
    return resolved.kind === "unresolved"
      ? { kind: "unresolved", path: null, host: null, raw_target: target.raw, reason: target.reason ?? null }
      : carryEndpointExpressionDetails(resolved, target);
  }
  const resolved = {
    kind: "host_unresolved",
    path: normalizeConfiguredPath(targetValue),
    host: null,
    raw_target: rawTarget,
    reason,
    host_resolution_kind: "expression_unresolved",
  };
  if (typeof unresolvedBaseRaw === "string" && unresolvedBaseRaw.length > 0) resolved[unresolvedBaseRawKey] = unresolvedBaseRaw;
  return carryEndpointExpressionDetails(resolved, target);
}

function targetWithUnresolvedHostPath(targetNode, sourceFile, bindings, reason, unresolvedBaseRaw = null, unresolvedBaseRawKey = "host_raw") {
  const target = resolveEndpointExpression(targetNode, sourceFile, bindings, new Set(), null, { relativePathAllowed: true });
  return targetWithUnresolvedHostPathExpression(
    target,
    rawNodeText(targetNode, sourceFile),
    reason,
    unresolvedBaseRaw,
    unresolvedBaseRawKey
  );
}

function targetWithHostValue(targetNode, hostValue, sourceFile, bindings, envNames = []) {
  const target = resolveEndpointExpression(targetNode, sourceFile, bindings, new Set(), null, { relativePathAllowed: true });
  if (target.kind === "unresolved" || target.value == null) {
    return { kind: "unresolved", path: null, host: null, raw_target: target.raw, reason: target.reason ?? null };
  }
  const targetValue = String(target.value).trim();
  if (targetValue.startsWith("http://") || targetValue.startsWith("https://") || targetValue.startsWith("${env:")) {
    return resolveEndpointTarget(targetNode, sourceFile, bindings);
  }
  const trimmedHost = String(hostValue).trim();
  if (trimmedHost.startsWith("http://") || trimmedHost.startsWith("https://") || trimmedHost.startsWith("${env:")) {
    const combined = `${trimTrailingSlashes(trimmedHost)}/${trimLeadingSlashes(targetValue)}`;
    const resolved = splitResolvedEndpointTarget(combined);
    if (envNames.length > 0) resolved.env_names = envNames;
    return carryEndpointExpressionDetails(resolved, target);
  }
  if (trimmedHost.length > 0) {
    const resolved = {
      kind: "resolved",
      path: normalizeConfiguredPath(targetValue),
      host: trimmedHost,
      raw_target: target.raw,
    };
    return carryEndpointExpressionDetails(resolved, target);
  }
  const resolved = splitResolvedEndpointTarget(normalizeConfiguredPath(targetValue));
  return carryEndpointExpressionDetails(resolved, target);
}

function thisMemberName(node) {
  const expression = ts.isParenthesizedExpression(node) ? node.expression : node;
  if (ts.isPropertyAccessExpression(expression) && expression.expression.kind === ts.SyntaxKind.ThisKeyword) {
    return expression.name.text;
  }
  if (
    ts.isElementAccessExpression(expression) &&
    expression.expression.kind === ts.SyntaxKind.ThisKeyword &&
    ts.isStringLiteral(expression.argumentExpression)
  ) {
    return expression.argumentExpression.text;
  }
  return null;
}

function resolvedStringExpression(node, sourceFile, bindings, parameterBindings = null) {
  const resolved = resolveEndpointExpression(node, sourceFile, bindings, new Set(), parameterBindings);
  if ((resolved.kind === "resolved" || resolved.kind === "env") && typeof resolved.value === "string") return resolved;
  return null;
}

function metadataExpressionFromDefaults(defaults, key) {
  if (!defaults || typeof defaults[key] !== "string") return null;
  const envNames = Array.isArray(defaults[`${key}_env_names`]) ? defaults[`${key}_env_names`] : [];
  const result = expressionResult(envNames.length > 0 ? "env" : "resolved", defaults[key], defaults[key], "class_default");
  if (envNames.length > 0) result.env_names = uniqueStrings(envNames);
  return result;
}

function copyClassExpressionWithRaw(expression, raw, resolutionKind) {
  const copied = copyExpressionWithRaw(expression, raw);
  copied.resolution_kind = resolutionKind;
  return copied;
}

function resolveEndpointExpressionWithClassContext(
  node,
  sourceFile,
  bindings,
  classContext = null,
  metadataKey = null,
  options = {}
) {
  const memberName = classContext == null ? null : thisMemberName(node);
  if (memberName != null) {
    const raw = rawNodeText(node, sourceFile);
    const memberValue = classContext.memberValues.get(memberName);
    if (memberValue != null) return copyClassExpressionWithRaw(memberValue, raw, "class_member");
    if (metadataKey != null && !classContext.inheritedDefaultBlockedMembers.has(memberName)) {
      // Base HTTP clients commonly expose the constructor service/base as an inherited member.
      const defaultValue = metadataExpressionFromDefaults(classContext.defaults, metadataKey);
      if (defaultValue != null) return copyClassExpressionWithRaw(defaultValue, raw, "class_inherited_default");
    }
  }
  return resolveEndpointExpression(node, sourceFile, bindings, new Set(), null, options);
}

function metadataKeyForRawKey(rawKey) {
  if (rawKey === "service_raw") return "service";
  if (rawKey === "base_url_raw") return "base_url";
  return "host";
}

function targetWithHostLikeBase(targetNode, hostNode, sourceFile, bindings, unresolvedBaseRawKey = "host_raw", classContext = null) {
  const host = resolveEndpointExpressionWithClassContext(
    hostNode,
    sourceFile,
    bindings,
    classContext,
    metadataKeyForRawKey(unresolvedBaseRawKey)
  );
  if (host.kind === "resolved" || host.kind === "env") {
    return targetWithHostValue(targetNode, host.value, sourceFile, bindings, Array.isArray(host.env_names) ? host.env_names : []);
  }
  return targetWithUnresolvedHostPath(
    targetNode,
    sourceFile,
    bindings,
    "host_or_service_unresolved",
    host.raw ?? rawNodeText(hostNode, sourceFile),
    unresolvedBaseRawKey
  );
}

function resolvedMetadataValue(node, sourceFile, bindings, classContext = null, key = null) {
  const resolved = resolveEndpointExpressionWithClassContext(node, sourceFile, bindings, classContext, key);
  if ((resolved.kind === "resolved" || resolved.kind === "env") && typeof resolved.value === "string") return resolved;
  return null;
}

function addConfigMetadataValue(metadata, key, node, sourceFile, bindings, classContext = null) {
  if (!node) return;
  const resolved = resolvedMetadataValue(node, sourceFile, bindings, classContext, key);
  if (resolved != null) {
    metadata[key] = resolved.value;
    if (Array.isArray(resolved.env_names)) metadata[`${key}_env_names`] = uniqueStrings(resolved.env_names);
    if (typeof resolved.resolution_kind === "string" && resolved.resolution_kind.startsWith("class_")) {
      metadata[`${key}_resolution_kind`] = resolved.resolution_kind;
    }
    return;
  }
  metadata[`${key}_raw`] = rawNodeText(node, sourceFile);
  if (rawIdentifierCanResolveThroughImport(node, sourceFile)) {
    metadata[`${key}_raw_imported_literal_candidate`] = true;
  }
}

function endpointConfigMetadata(configNode, sourceFile, bindings, defaults = {}, classContext = null) {
  const metadata = { ...defaults };
  addConfigMetadataValue(metadata, "service", objectLiteralProperty(configNode, "service"), sourceFile, bindings, classContext);
  addConfigMetadataValue(metadata, "api_version", objectLiteralProperty(configNode, "apiVersion"), sourceFile, bindings, classContext);
  addConfigMetadataValue(metadata, "client_app_id", objectLiteralProperty(configNode, "clientAppId"), sourceFile, bindings, classContext);
  return metadata;
}

function endpointConfigDefaults(configNode, sourceFile, bindings) {
  const defaults = endpointConfigMetadata(configNode, sourceFile, bindings);
  const baseUrlProperty = endpointConfigBaseUrlProperty(configNode);
  addConfigMetadataValue(defaults, "base_url", baseUrlProperty?.initializer, sourceFile, bindings);
  addConfigMetadataValue(defaults, "host", objectLiteralProperty(configNode, "host"), sourceFile, bindings);
  return defaults;
}

function endpointDefaultBase(defaults) {
  if (typeof defaults.base_url === "string") {
    return {
      value: defaults.base_url,
      env_names: Array.isArray(defaults.base_url_env_names) ? defaults.base_url_env_names : [],
    };
  }
  if (typeof defaults.host === "string") {
    return {
      value: defaults.host,
      env_names: Array.isArray(defaults.host_env_names) ? defaults.host_env_names : [],
    };
  }
  if (typeof defaults.service === "string") {
    return {
      value: defaults.service,
      env_names: Array.isArray(defaults.service_env_names) ? defaults.service_env_names : [],
    };
  }
  return null;
}

function endpointDefaultsHaveUnresolvedBase(defaults) {
  return (
    typeof defaults.base_url_raw === "string" ||
    typeof defaults.host_raw === "string" ||
    typeof defaults.service_raw === "string"
  );
}

function endpointDefaultUnresolvedBase(defaults) {
  if (typeof defaults.base_url_raw === "string") return { raw: defaults.base_url_raw, rawKey: "base_url_raw" };
  if (typeof defaults.host_raw === "string") return { raw: defaults.host_raw, rawKey: "host_raw" };
  if (typeof defaults.service_raw === "string") return { raw: defaults.service_raw, rawKey: "service_raw" };
  return null;
}

function endpointTargetFromConfig(configNode, sourceFile, bindings, defaults = {}, classContext = null) {
  if (!ts.isObjectLiteralExpression(configNode)) return null;
  const targetProperty = endpointConfigTargetProperty(configNode);
  if (!targetProperty) return null;

  const baseUrlProperty = endpointConfigBaseUrlProperty(configNode);
  let target;
  if (baseUrlProperty) {
    target = composedTargetWithBaseUrl(
      targetProperty.initializer,
      sourceFile,
      bindings,
      resolveEndpointExpressionWithClassContext(baseUrlProperty.initializer, sourceFile, bindings, classContext, "base_url")
    );
  } else {
    const hostProperty = objectLiteralPropertyFromNames(configNode, ["host", "service"]);
    const defaultBase = endpointDefaultBase(defaults);
    if (hostProperty) {
      target = targetWithHostLikeBase(
        targetProperty.initializer,
        hostProperty.initializer,
        sourceFile,
        bindings,
        `${hostProperty.name}_raw`,
        classContext
      );
    } else if (defaultBase != null) {
      target = targetWithHostValue(targetProperty.initializer, defaultBase.value, sourceFile, bindings, defaultBase.env_names);
    } else if (endpointDefaultsHaveUnresolvedBase(defaults)) {
      const unresolvedDefaultBase = endpointDefaultUnresolvedBase(defaults);
      target = targetWithUnresolvedHostPath(
        targetProperty.initializer,
        sourceFile,
        bindings,
        "host_or_service_unresolved",
        unresolvedDefaultBase?.raw,
        unresolvedDefaultBase?.rawKey ?? "host_raw"
      );
    } else {
      target = resolveEndpointTarget(targetProperty.initializer, sourceFile, bindings);
    }
  }
  const metadata = endpointConfigMetadata(configNode, sourceFile, bindings, defaults, classContext);
  return {
    target,
    method: methodFromOptionsLike(configNode),
    metadata,
  };
}

function rowFromTarget(target, method, line, sourceKind, metadata = {}) {
  const extra = metadata && typeof metadata === "object" ? metadata : {};
  const targetExtra = {};
  for (const key of ["host_raw", "service_raw", "base_url_raw"]) {
    if (typeof target[key] === "string" && target[key].length > 0) targetExtra[key] = target[key];
  }
  if (target.kind === "external") {
    return { ...extra, ...targetExtra, external: true, host: target.host, path: target.path, raw_target: target.raw_target, line, source_kind: sourceKind };
  }
  if (target.kind === "unresolved") {
    return { ...extra, ...targetExtra, unresolved: true, raw_target: target.raw_target, line, source_kind: sourceKind, reason: target.reason ?? null };
  }
  return {
    ...extra,
    ...targetExtra,
    method: method ?? "ANY",
    path: target.path,
    host: target.host,
    raw_target: target.raw_target,
    line,
    source_kind: sourceKind,
    confidence: target.kind === "host_unresolved" ? "host_unresolved_path_resolved" : null,
    reason: target.reason ?? null,
    resolution_kind: target.resolution_kind ?? null,
    host_resolution_kind: target.host_resolution_kind ?? null,
    route_params: Array.isArray(target.route_params) ? target.route_params : null,
    env_names: Array.isArray(target.env_names) ? target.env_names : null,
  };
}

function axiosConfigTarget(configNode, sourceFile, bindings, baseUrlExpression) {
  if (!ts.isObjectLiteralExpression(configNode)) return { target: { kind: "unresolved", path: null, host: null, raw_target: rawNodeText(configNode, sourceFile) }, method: null };
  const urlNode = objectLiteralProperty(configNode, "url");
  if (!urlNode) return { target: { kind: "unresolved", path: null, host: null, raw_target: rawNodeText(configNode, sourceFile) }, method: null };
  const baseUrlNode = objectLiteralProperty(configNode, "baseURL");
  const effectiveBaseUrl = baseUrlNode ? resolveEndpointExpression(baseUrlNode, sourceFile, bindings) : baseUrlExpression;
  return {
    target: composedTargetWithBaseUrl(urlNode, sourceFile, bindings, effectiveBaseUrl),
    method: methodFromOptionsLike(configNode),
  };
}

function importedBindingsByLocal(sourceFile) {
  const bindings = new Map();
  const duplicateLocals = new Set();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    if (statement.moduleSpecifier.text === "axios") continue;
    const clause = statement.importClause;
    if (!clause || clause.isTypeOnly) continue;

    function addBinding(localName, importedName) {
      if (bindings.has(localName)) duplicateLocals.add(localName);
      bindings.set(localName, { import_source: statement.moduleSpecifier.text, imported_name: importedName });
    }

    if (clause.name) addBinding(clause.name.text, "default");
    if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const element of clause.namedBindings.elements) {
        addBinding(element.name.text, (element.propertyName ?? element.name).text);
      }
    }
    if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
      addBinding(clause.namedBindings.name.text, clause.namedBindings.name.text);
    }
  }
  for (const localName of duplicateLocals) bindings.delete(localName);
  return bindings;
}

function targetExpressionFromConfig(configNode, sourceFile, bindings) {
  if (!ts.isObjectLiteralExpression(configNode)) return { target: { kind: "unresolved", value: null, raw: rawNodeText(configNode, sourceFile) }, method: null };
  const urlNode = objectLiteralProperty(configNode, "url");
  if (!urlNode) return { target: { kind: "unresolved", value: null, raw: rawNodeText(configNode, sourceFile) }, method: null };
  const baseUrlNode = objectLiteralProperty(configNode, "baseURL");
  return {
    target: resolveEndpointExpression(urlNode, sourceFile, bindings),
    method: methodFromOptionsLike(configNode),
    base_url: baseUrlNode ? resolveEndpointExpression(baseUrlNode, sourceFile, bindings) : null,
  };
}

function importedClientCallFromNode(node, sourceFile, importedBindings, bindings) {
  if (!ts.isCallExpression(node)) return null;

  if (ts.isIdentifier(node.expression) && importedBindings.has(node.expression.text)) {
    const receiver = node.expression.text;
    if (identifierIsLocallyShadowed(node.expression, receiver, sourceFile) || node.arguments.length < 1) return null;
    const binding = importedBindings.get(receiver);
    const firstArg = node.arguments[0];
    const target = ts.isObjectLiteralExpression(firstArg)
      ? targetExpressionFromConfig(firstArg, sourceFile, bindings)
      : { target: resolveEndpointExpression(firstArg, sourceFile, bindings), method: "GET" };
    return {
      source_kind: "imported_axios_call",
      receiver_local: receiver,
      imported_name: binding.imported_name,
      import_source: binding.import_source,
      method: target.method ?? (ts.isObjectLiteralExpression(firstArg) ? "ANY" : "GET"),
      target: target.target,
      base_url: target.base_url ?? null,
      raw_target: target.target.raw,
      line: lineOf(sourceFile, node.expression.getStart(sourceFile)),
    };
  }

  if (!ts.isPropertyAccessExpression(node.expression) || !ts.isIdentifier(node.expression.expression)) return null;
  const receiver = node.expression.expression.text;
  if (!importedBindings.has(receiver) || identifierIsLocallyShadowed(node.expression.expression, receiver, sourceFile)) return null;

  const property = node.expression.name.text;
  if (property === "request") {
    if (node.arguments.length < 1) return null;
    const target = targetExpressionFromConfig(node.arguments[0], sourceFile, bindings);
    const binding = importedBindings.get(receiver);
    return {
      source_kind: "imported_axios_call",
      receiver_local: receiver,
      imported_name: binding.imported_name,
      import_source: binding.import_source,
      method: target.method ?? "ANY",
      target: target.target,
      base_url: target.base_url ?? null,
      raw_target: target.target.raw,
      line: lineOf(sourceFile, node.expression.getStart(sourceFile)),
    };
  }
  if (!HTTP_METHODS.has(property) || node.arguments.length < 1) return null;

  const binding = importedBindings.get(receiver);
  const target = resolveEndpointExpression(node.arguments[0], sourceFile, bindings);
  return {
    source_kind: "imported_axios_call",
    receiver_local: receiver,
    imported_name: binding.imported_name,
    import_source: binding.import_source,
    method: property.toUpperCase(),
    target,
    raw_target: target.raw,
    line: lineOf(sourceFile, node.expression.getStart(sourceFile)),
  };
}

function classMemberAssignment(node) {
  if (!ts.isBinaryExpression(node) || !ASSIGNMENT_OPERATORS.has(node.operatorToken.kind)) return null;
  const name = thisMemberName(node.left);
  if (name == null) return null;
  return { name, initializer: node.right, operatorKind: node.operatorToken.kind };
}

function classMemberUpdateName(node) {
  if (
    (ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) &&
    (node.operator === ts.SyntaxKind.PlusPlusToken || node.operator === ts.SyntaxKind.MinusMinusToken)
  ) {
    return thisMemberName(node.operand);
  }
  return null;
}

function classMemberWriteEntry(writes, name) {
  if (!writes.has(name)) writes.set(name, { total: 0, constructor: 0, nonConstructor: 0 });
  return writes.get(name);
}

function collectClassMemberWriteInfo(classNode) {
  const writes = new Map();
  const constructorAssignments = [];
  for (const member of classNode.members) {
    const inConstructor = ts.isConstructorDeclaration(member);
    if (!inConstructor && isStaticClassElement(member)) continue;
    function recordWrite(name) {
      const entry = classMemberWriteEntry(writes, name);
      entry.total += 1;
      if (inConstructor) entry.constructor += 1;
      else entry.nonConstructor += 1;
    }
    function visit(node) {
      if (node !== member && (ts.isClassDeclaration(node) || ts.isClassExpression(node))) return;
      if (node !== member && isThisRebindingFunctionBoundary(node)) return;
      const assignment = classMemberAssignment(node);
      if (assignment != null) {
        recordWrite(assignment.name);
        if (inConstructor && assignment.operatorKind === ts.SyntaxKind.EqualsToken) {
          constructorAssignments.push({ ...assignment, constructorNode: member });
        }
      }
      const updateName = classMemberUpdateName(node);
      if (updateName != null) recordWrite(updateName);
      ts.forEachChild(node, visit);
    }
    visit(member);
  }
  return { writes, constructorAssignments };
}

function parameterDeclaresClassProperty(parameter) {
  return Array.from(parameter.modifiers ?? []).some((modifier) =>
    [
      ts.SyntaxKind.PublicKeyword,
      ts.SyntaxKind.PrivateKeyword,
      ts.SyntaxKind.ProtectedKeyword,
      ts.SyntaxKind.ReadonlyKeyword,
    ].includes(modifier.kind)
  );
}

function constructorParameterBindings(constructorNode, sourceFile, bindings) {
  const parameterBindings = new Map();
  for (const parameter of constructorNode.parameters ?? []) {
    if (!ts.isIdentifier(parameter.name) || parameter.initializer == null) continue;
    const resolved = resolvedStringExpression(parameter.initializer, sourceFile, bindings);
    if (resolved != null) parameterBindings.set(parameter.name.text, resolved);
  }
  return parameterBindings;
}

function recordClassMemberValue(values, ambiguous, name, expression) {
  if (ambiguous.has(name)) return;
  if (values.has(name)) {
    values.delete(name);
    ambiguous.add(name);
    return;
  }
  values.set(name, expression);
}

function classInstanceMemberContext(classNode, sourceFile, bindings) {
  const { writes, constructorAssignments } = collectClassMemberWriteInfo(classNode);
  const memberValues = new Map();
  const ambiguous = new Set();
  const declaredMembers = new Set();

  for (const member of classNode.members) {
    if (ts.isPropertyDeclaration(member) && !nodeHasModifier(member, ts.SyntaxKind.StaticKeyword)) {
      const name = propertyNameText(member.name);
      if (name == null) continue;
      declaredMembers.add(name);
      if (member.initializer == null || writes.has(name)) continue;
      const resolved = resolvedStringExpression(member.initializer, sourceFile, bindings);
      if (resolved != null) recordClassMemberValue(memberValues, ambiguous, name, resolved);
    }

    if (!ts.isConstructorDeclaration(member)) continue;
    for (const parameter of member.parameters ?? []) {
      if (!parameterDeclaresClassProperty(parameter) || !ts.isIdentifier(parameter.name)) continue;
      const name = parameter.name.text;
      declaredMembers.add(name);
      if (parameter.initializer == null || writes.has(name)) continue;
      const resolved = resolvedStringExpression(parameter.initializer, sourceFile, bindings);
      if (resolved != null) recordClassMemberValue(memberValues, ambiguous, name, resolved);
    }
  }

  const constructorAssignmentsByName = new Map();
  for (const assignment of constructorAssignments) {
    if (!constructorAssignmentsByName.has(assignment.name)) constructorAssignmentsByName.set(assignment.name, []);
    constructorAssignmentsByName.get(assignment.name).push(assignment);
  }
  for (const [name, assignments] of constructorAssignmentsByName.entries()) {
    const writeInfo = writes.get(name);
    if (assignments.length !== 1 || writeInfo?.total !== 1 || writeInfo.nonConstructor !== 0 || memberValues.has(name)) {
      memberValues.delete(name);
      ambiguous.add(name);
      continue;
    }
    const assignment = assignments[0];
    const parameterBindings = constructorParameterBindings(assignment.constructorNode, sourceFile, bindings);
    const resolved = resolvedStringExpression(assignment.initializer, sourceFile, bindings, parameterBindings);
    if (resolved != null) recordClassMemberValue(memberValues, ambiguous, name, resolved);
  }

  for (const name of ambiguous) memberValues.delete(name);
  return {
    memberValues,
    inheritedDefaultBlockedMembers: new Set([...declaredMembers, ...writes.keys(), ...ambiguous]),
  };
}

function addResolvedDefault(defaults, key, resolved) {
  defaults[key] = resolved.value;
  if (Array.isArray(resolved.env_names)) defaults[`${key}_env_names`] = uniqueStrings(resolved.env_names);
}

function classEndpointContext(classNode, sourceFile, bindings, defaults = null) {
  const resolvedDefaults = defaults ?? classSuperEndpointDefaults(classNode, sourceFile, bindings) ?? {};
  const memberContext = classInstanceMemberContext(classNode, sourceFile, bindings);
  return {
    defaults: resolvedDefaults,
    memberValues: memberContext.memberValues,
    inheritedDefaultBlockedMembers: memberContext.inheritedDefaultBlockedMembers,
  };
}

function importedHttpWrapperCallFromNode(node, sourceFile, importedBindings, bindings, classContext = null) {
  if (!ts.isCallExpression(node) || node.arguments.length < 1 || !ts.isObjectLiteralExpression(node.arguments[0])) return null;
  const configNode = node.arguments[0];
  if (endpointConfigTargetProperty(configNode) == null || !endpointConfigHasWrapperContext(configNode)) return null;
  if (endpointConfigLooksLikeAxiosRequestConfig(configNode)) return null;

  let method = null;
  let receiverLocal = null;
  let binding = null;
  let wrapperMethod = null;
  if (ts.isIdentifier(node.expression) && importedBindings.has(node.expression.text)) {
    receiverLocal = node.expression.text;
    if (identifierIsLocallyShadowed(node.expression, receiverLocal, sourceFile)) return null;
    binding = importedBindings.get(receiverLocal);
    method = httpWrapperMethodName(binding.imported_name) ?? httpWrapperMethodName(receiverLocal);
  } else if (ts.isPropertyAccessExpression(node.expression) && ts.isIdentifier(node.expression.expression)) {
    receiverLocal = node.expression.expression.text;
    if (!importedBindings.has(receiverLocal) || identifierIsLocallyShadowed(node.expression.expression, receiverLocal, sourceFile)) return null;
    binding = importedBindings.get(receiverLocal);
    wrapperMethod = node.expression.name.text;
    method = httpWrapperMethodName(wrapperMethod);
  } else {
    return null;
  }

  const resolved = endpointTargetFromConfig(configNode, sourceFile, bindings, {}, classContext);
  if (!resolved) return null;
  if (method == null) return null;
  const metadata = {
    ...resolved.metadata,
    wrapper_receiver: receiverLocal,
    wrapper_import_source: binding.import_source,
    wrapper_imported_name: binding.imported_name,
  };
  if (wrapperMethod != null) metadata.wrapper_method = wrapperMethod;
  return rowFromTarget(
    resolved.target,
    resolved.method ?? method ?? "ANY",
    lineOf(sourceFile, node.expression.getStart(sourceFile)),
    "http_wrapper_call",
    metadata
  );
}

function classSuperEndpointDefaults(classNode, sourceFile, bindings) {
  const defaults = {};
  for (const member of classNode.members) {
    if (!ts.isConstructorDeclaration(member) || !member.body) continue;
    const directSuperCalls = [];
    for (const statement of member.body.statements ?? []) {
      if (
        ts.isExpressionStatement(statement) &&
        ts.isCallExpression(statement.expression) &&
        statement.expression.expression.kind === ts.SyntaxKind.SuperKeyword
      ) {
        directSuperCalls.push(statement.expression);
      }
    }
    if (directSuperCalls.length !== 1) continue;

    const superCall = directSuperCalls[0];
    if (superCall.arguments.length < 1) continue;
    if (ts.isObjectLiteralExpression(superCall.arguments[0]) && endpointConfigHasBaseContext(superCall.arguments[0])) {
      const configNode = superCall.arguments[0];
      const metadata = endpointConfigDefaults(configNode, sourceFile, bindings);
      for (const [key, value] of Object.entries(metadata)) {
        defaults[key] = value;
      }
      continue;
    }
    const serviceDefault = resolvedMetadataValue(superCall.arguments[0], sourceFile, bindings);
    if (serviceDefault != null) {
      addResolvedDefault(defaults, "service", serviceDefault);
    }
  }
  return Object.keys(defaults).length > 0 ? defaults : null;
}

function thisHttpWrapperCallFromNode(node, sourceFile, bindings, classContext) {
  if (
    !ts.isCallExpression(node) ||
    node.arguments.length < 1 ||
    !ts.isObjectLiteralExpression(node.arguments[0]) ||
    !ts.isPropertyAccessExpression(node.expression) ||
    node.expression.expression.kind !== ts.SyntaxKind.ThisKeyword
  ) {
    return null;
  }
  const method = httpWrapperMethodName(node.expression.name.text);
  if (method == null || endpointConfigTargetProperty(node.arguments[0]) == null) return null;
  const resolved = endpointTargetFromConfig(node.arguments[0], sourceFile, bindings, classContext.defaults, classContext);
  if (!resolved) return null;
  const metadata = {
    ...resolved.metadata,
    wrapper_receiver: "this",
    wrapper_method: node.expression.name.text,
  };
  return rowFromTarget(
    resolved.target,
    resolved.method ?? method,
    lineOf(sourceFile, node.expression.getStart(sourceFile)),
    "http_controller_wrapper_call",
    metadata
  );
}

function visitExecutableClassElement(member, classContext, visit) {
  if (ts.isConstructorDeclaration(member)) {
    if (member.body) visit(member.body, classContext, classContext != null);
    return;
  }
  if (ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member)) {
    if (member.body) visit(member.body, classContext, classContext != null);
    return;
  }
  if (ts.isPropertyDeclaration(member)) {
    if (member.initializer) visit(member.initializer, classContext, classContext != null);
    return;
  }
  if (isClassStaticBlock(member)) {
    visit(member, null, false);
  }
}

function collectHttpControllerWrapperCalls(sourceFile, bindings) {
  const calls = [];
  function visitClass(classNode) {
    const defaults = classSuperEndpointDefaults(classNode, sourceFile, bindings);
    if (defaults == null || Object.keys(defaults).length === 0) return;
    const classContext = classEndpointContext(classNode, sourceFile, bindings, defaults);
    function visit(node, preserveThisContext = false) {
      if (!preserveThisContext && isThisRebindingFunctionBoundary(node)) return;
      const call = thisHttpWrapperCallFromNode(node, sourceFile, bindings, classContext);
      if (call) calls.push(call);
      ts.forEachChild(node, (child) => visit(child, false));
    }
    for (const member of classNode.members) {
      if (!ts.isConstructorDeclaration(member) && !isStaticClassElement(member)) {
        visitExecutableClassElement(member, classContext, (node, _classContext, preserveThisContext) =>
          visit(node, preserveThisContext)
        );
      }
    }
  }
  function visit(node) {
    if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) {
      visitClass(node);
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return calls;
}

function clientCallFromNode(node, sourceFile, axiosLocals, axiosClients, axiosClientBaseUrls, bindings) {
  if (!ts.isCallExpression(node)) return null;
  if (ts.isIdentifier(node.expression) && node.expression.text === "fetch") {
    if (node.arguments.length < 1) return null;
    if (
      ts.isObjectLiteralExpression(node.arguments[0]) &&
      !objectLiteralHasDynamicProperty(node.arguments[0]) &&
      endpointConfigTargetProperty(node.arguments[0]) != null
    ) {
      const resolved = endpointTargetFromConfig(node.arguments[0], sourceFile, bindings);
      if (resolved) {
        return rowFromTarget(
          resolved.target,
          resolved.method,
          lineOf(sourceFile, node.expression.getStart(sourceFile)),
          "fetch_call",
          resolved.metadata
        );
      }
    }
    const target = resolveEndpointTarget(node.arguments[0], sourceFile, bindings, {
      leadingDynamicRelativePathAllowed: identifierIsLocallyShadowed(node.expression, "fetch", sourceFile),
    });
    const method = node.arguments.length >= 2 ? methodFromOptionsLike(node.arguments[1]) : null;
    return rowFromTarget(target, method, lineOf(sourceFile, node.expression.getStart(sourceFile)), "fetch_call");
  }

  if (ts.isIdentifier(node.expression) && axiosLocals.has(node.expression.text)) {
    if (node.arguments.length < 1) return null;
    const firstArg = node.arguments[0];
    if (!ts.isObjectLiteralExpression(firstArg)) {
      const target = resolveEndpointTarget(firstArg, sourceFile, bindings);
      return rowFromTarget(target, "GET", lineOf(sourceFile, node.expression.getStart(sourceFile)), "axios_call");
    }
    const { target, method } = axiosConfigTarget(firstArg, sourceFile, bindings, null);
    return rowFromTarget(target, method, lineOf(sourceFile, node.expression.getStart(sourceFile)), "axios_call");
  }

  if (ts.isIdentifier(node.expression) && axiosClients.has(node.expression.text)) {
    if (node.arguments.length < 1) return null;
    const firstArg = node.arguments[0];
    const baseUrl = axiosClientBaseUrls.get(node.expression.text);
    if (!ts.isObjectLiteralExpression(firstArg)) {
      const target = composedTargetWithBaseUrl(firstArg, sourceFile, bindings, baseUrl);
      return rowFromTarget(target, "GET", lineOf(sourceFile, node.expression.getStart(sourceFile)), "axios_call");
    }
    const { target, method } = axiosConfigTarget(firstArg, sourceFile, bindings, baseUrl);
    return rowFromTarget(target, method, lineOf(sourceFile, node.expression.getStart(sourceFile)), "axios_call");
  }

  if (!ts.isPropertyAccessExpression(node.expression) || !ts.isIdentifier(node.expression.expression)) return null;
  const receiver = node.expression.expression.text;
  if (!axiosLocals.has(receiver) && !axiosClients.has(receiver)) return null;
  const property = node.expression.name.text;
  if (property === "request") {
    if (node.arguments.length < 1) return null;
    const baseUrl = axiosClients.has(receiver) ? axiosClientBaseUrls.get(receiver) : null;
    const { target, method } = axiosConfigTarget(node.arguments[0], sourceFile, bindings, baseUrl);
    return rowFromTarget(target, method, lineOf(sourceFile, node.expression.getStart(sourceFile)), "axios_call");
  }
  if (!HTTP_METHODS.has(property) || node.arguments.length < 1) return null;
  const baseUrl = axiosClients.has(receiver) ? axiosClientBaseUrls.get(receiver) : null;
  const target = composedTargetWithBaseUrl(node.arguments[0], sourceFile, bindings, baseUrl);
  return rowFromTarget(target, property.toUpperCase(), lineOf(sourceFile, node.expression.getStart(sourceFile)), "axios_call");
}

function collectClientEndpointCalls(sourceFile) {
  const axiosLocals = collectAxiosLocals(sourceFile);
  const bindings = collectTopLevelLiteralBindings(sourceFile);
  const { clients: axiosClients, baseUrls: axiosClientBaseUrls } = collectAxiosClients(sourceFile, axiosLocals, bindings);
  const importedBindings = importedBindingsByLocal(sourceFile);
  const calls = collectHttpControllerWrapperCalls(sourceFile, bindings);
  function visit(node, classContext = null, preserveThisContext = false) {
    if (ts.isClassDeclaration(node) || ts.isClassExpression(node)) {
      const nextClassContext = classEndpointContext(node, sourceFile, bindings);
      for (const decorator of nodeDecorators(node)) visit(decorator, null, false);
      for (const heritageClause of node.heritageClauses ?? []) visit(heritageClause, null, false);
      for (const member of node.members) {
        for (const decorator of nodeDecorators(member)) visit(decorator, null, false);
        if (member.name && ts.isComputedPropertyName(member.name)) visit(member.name, null, false);
        const memberClassContext = isStaticClassElement(member) ? null : nextClassContext;
        visitExecutableClassElement(member, memberClassContext, visit);
      }
      return;
    }
    const effectiveClassContext =
      classContext != null && !preserveThisContext && isThisRebindingFunctionBoundary(node) ? null : classContext;
    const call = clientCallFromNode(node, sourceFile, axiosLocals, axiosClients, axiosClientBaseUrls, bindings);
    if (call) calls.push(call);
    const wrapperCall = importedHttpWrapperCallFromNode(node, sourceFile, importedBindings, bindings, effectiveClassContext);
    if (wrapperCall) {
      calls.push(wrapperCall);
    } else {
      const importedCall = importedClientCallFromNode(node, sourceFile, importedBindings, bindings);
      if (importedCall) calls.push(importedCall);
    }
    ts.forEachChild(node, (child) => visit(child, effectiveClassContext, false));
  }
  visit(sourceFile);
  return calls;
}

function symbolFromStatement(statement, sourceFile) {
  if (ts.isFunctionDeclaration(statement) && statement.name) {
    return { name: statement.name.text, kind: "function", line: lineOf(sourceFile, statement.name.getStart(sourceFile)), end_line: lineOf(sourceFile, statement.end), pos: statement.pos, end: statement.end };
  }
  if (ts.isClassDeclaration(statement) && statement.name) {
    return { name: statement.name.text, kind: "class", line: lineOf(sourceFile, statement.name.getStart(sourceFile)), end_line: lineOf(sourceFile, statement.end), pos: statement.pos, end: statement.end };
  }
  if (ts.isInterfaceDeclaration(statement)) {
    return { name: statement.name.text, kind: "interface", line: lineOf(sourceFile, statement.name.getStart(sourceFile)), end_line: lineOf(sourceFile, statement.end), pos: statement.pos, end: statement.end };
  }
  if (ts.isTypeAliasDeclaration(statement)) {
    return { name: statement.name.text, kind: "type", line: lineOf(sourceFile, statement.name.getStart(sourceFile)), end_line: lineOf(sourceFile, statement.end), pos: statement.pos, end: statement.end };
  }
  if (ts.isEnumDeclaration(statement)) {
    return { name: statement.name.text, kind: "enum", line: lineOf(sourceFile, statement.name.getStart(sourceFile)), end_line: lineOf(sourceFile, statement.end), pos: statement.pos, end: statement.end };
  }
  return null;
}

function collectSymbols(sourceFile) {
  const symbols = [];
  for (const statement of sourceFile.statements) {
    const symbol = symbolFromStatement(statement, sourceFile);
    if (symbol) {
      symbols.push(symbol);
      continue;
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        for (const name of bindingNames(declaration.name)) {
          const kind = declaration.initializer && (ts.isArrowFunction(declaration.initializer) || ts.isFunctionExpression(declaration.initializer)) ? "function" : "value";
          symbols.push({
            name,
            kind,
            line: lineOf(sourceFile, declaration.name.getStart(sourceFile)),
            end_line: lineOf(sourceFile, declaration.end),
            pos: declaration.pos,
            end: declaration.end,
          });
        }
      }
    }
  }
  return symbols;
}

function collectCallsForSymbol(sourceFile, symbol) {
  const calls = [];
  function visit(node) {
    if (node !== sourceFile && (node.pos < symbol.pos || node.end > symbol.end)) return;
    if (ts.isCallExpression(node)) {
      const name = callName(node.expression, sourceFile);
      if (name) {
        calls.push({ name, line: lineOf(sourceFile, node.expression.getStart(sourceFile)) });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return calls;
}

const NEST_CLIENT_TYPES = new Set([
  "ClientProxy",
  "ClientKafka",
  "ClientRMQ",
  "ClientTCP",
  "ClientRedis",
  "ClientMqtt",
  "ClientNats",
  "ClientGrpcProxy",
]);
const NEST_CONSUMER_DECORATORS = new Set(["EventPattern", "MessagePattern"]);
const NEST_PRODUCER_METHODS = new Set(["emit", "send"]);

function nodeDecorators(node) {
  if (typeof ts.canHaveDecorators === "function" && typeof ts.getDecorators === "function") {
    if (!ts.canHaveDecorators(node)) return [];
    return ts.getDecorators(node) || [];
  }
  return node.decorators || [];
}

function decoratorCall(decorator) {
  const expr = decorator.expression;
  if (expr && ts.isCallExpression(expr) && ts.isIdentifier(expr.expression)) {
    return { name: expr.expression.text, args: expr.arguments };
  }
  return null;
}

function simpleTypeName(typeNode) {
  if (typeNode && ts.isTypeReferenceNode(typeNode)) {
    let name = typeNode.typeName;
    while (name && name.right) name = name.right;
    if (name && name.text) return name.text;
  }
  return null;
}

function classClientMembers(classNode) {
  const members = new Map();
  const accessibility = new Set([
    ts.SyntaxKind.PrivateKeyword,
    ts.SyntaxKind.PublicKeyword,
    ts.SyntaxKind.ProtectedKeyword,
    ts.SyntaxKind.ReadonlyKeyword,
  ]);
  for (const member of classNode.members) {
    if (ts.isConstructorDeclaration(member)) {
      for (const param of member.parameters) {
        const isParamProperty = param.modifiers && param.modifiers.some((m) => accessibility.has(m.kind));
        if (isParamProperty && ts.isIdentifier(param.name)) {
          const typeName = simpleTypeName(param.type);
          if (typeName) members.set(param.name.text, typeName);
        }
      }
    } else if (ts.isPropertyDeclaration(member) && ts.isIdentifier(member.name)) {
      const typeName = simpleTypeName(member.type);
      if (typeName) members.set(member.name.text, typeName);
    }
  }
  return members;
}

function receiverMemberName(expression) {
  // Only `this.<member>` reliably refers to the class's injected client; a bare identifier in a
  // method body is a local/parameter/import, so matching it would misattribute the member type.
  if (ts.isPropertyAccessExpression(expression) && expression.expression.kind === ts.SyntaxKind.ThisKeyword) {
    return expression.name.text;
  }
  return null;
}

function channelFromArg(argNode, sourceFile) {
  if (argNode == null) {
    return { channel: null, channel_raw: null, reason: "missing_channel_arg" };
  }
  const literal = stringLiteralValue(argNode);
  if (literal != null) {
    return { channel: literal, channel_raw: literal, reason: null };
  }
  return { channel: null, channel_raw: rawNodeText(argNode, sourceFile), reason: "non_literal_channel" };
}

// NestJS microservice events: `@EventPattern`/`@MessagePattern` consumers and
// ClientProxy/ClientKafka `.emit`/`.send` producers. Gated on a `@nestjs/microservices`
// import; producers require the receiver member to be typed as a Nest client (so the very
// generic `.emit`/`.send` method names aren't matched on unrelated objects).
function collectMessageEvents(sourceFile) {
  const importsNest = sourceFile.statements.some(
    (statement) =>
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text === "@nestjs/microservices"
  );
  if (!importsNest) return [];
  const events = [];

  function visitClass(classNode, className) {
    const clientMembers = classClientMembers(classNode);
    function visit(node) {
      if (ts.isMethodDeclaration(node)) {
        for (const decorator of nodeDecorators(node)) {
          const info = decoratorCall(decorator);
          if (info && NEST_CONSUMER_DECORATORS.has(info.name)) {
            const channel = channelFromArg(info.args[0], sourceFile);
            events.push({
              predicate: "CONSUMES_EVENT",
              broker: "nestjs",
              subject_class: className,
              api: `@${info.name}`,
              line: lineOf(sourceFile, node.getStart(sourceFile)),
              ...channel,
            });
          }
        }
      }
      if (
        ts.isCallExpression(node) &&
        ts.isPropertyAccessExpression(node.expression) &&
        NEST_PRODUCER_METHODS.has(node.expression.name.text)
      ) {
        const member = receiverMemberName(node.expression.expression);
        if (member && NEST_CLIENT_TYPES.has(clientMembers.get(member))) {
          const channel = channelFromArg(node.arguments[0], sourceFile);
          events.push({
            predicate: "PRODUCES_EVENT",
            broker: "nestjs",
            subject_class: className,
            api: `ClientProxy.${node.expression.name.text}`,
            line: lineOf(sourceFile, node.expression.name.getStart(sourceFile)),
            ...channel,
          });
        }
      }
      node.forEachChild(visit);
    }
    visit(classNode);
  }

  function topVisit(node) {
    if (ts.isClassDeclaration(node) && node.name) visitClass(node, node.name.text);
    node.forEachChild(topVisit);
  }
  topVisit(sourceFile);
  return events;
}

const NEST_HTTP_DECORATORS = {
  Get: "get",
  Post: "post",
  Put: "put",
  Patch: "patch",
  Delete: "delete",
  Options: "options",
  Head: "head",
  All: "all",
};

function importsModule(sourceFile, moduleName) {
  return sourceFile.statements.some(
    (statement) =>
      ts.isImportDeclaration(statement) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text === moduleName
  );
}

function joinNestRoute(prefix, path) {
  const segments = (value) => (value || "").split("/").filter(Boolean);
  const parts = [...segments(prefix), ...segments(path)];
  return "/" + parts.join("/");
}

// NestJS HTTP controllers: `@Controller('prefix')` class + `@Get/@Post/...('path')` methods.
// Gated on a `@nestjs/common` import. Emitted in the express-route shape so the existing route
// adapter turns them into EXPOSES_ENDPOINT. Non-literal route templates are skipped.
function collectNestRoutes(sourceFile) {
  if (!importsModule(sourceFile, "@nestjs/common")) return [];
  const routes = [];

  function visitClass(classNode) {
    let prefix; // undefined = not a controller; null = non-literal prefix (skip whole controller)
    for (const decorator of nodeDecorators(classNode)) {
      const info = decoratorCall(decorator);
      if (info && info.name === "Controller") {
        if (!info.args.length) prefix = "";
        else {
          const literal = stringLiteralValue(info.args[0]);
          prefix = literal == null ? null : literal;
        }
      }
    }
    // Not a controller, or a non-literal prefix we can't resolve (skip rather than emit a path
    // that's missing its real prefix).
    if (prefix === undefined || prefix === null) return;
    for (const member of classNode.members) {
      if (!ts.isMethodDeclaration(member)) continue;
      for (const decorator of nodeDecorators(member)) {
        const info = decoratorCall(decorator);
        if (!info) continue;
        const verb = NEST_HTTP_DECORATORS[info.name];
        if (!verb) continue;
        let methodPath = "";
        if (info.args.length) {
          const literal = stringLiteralValue(info.args[0]);
          if (literal == null) continue; // non-literal route template -> skip just this route
          methodPath = literal;
        }
        routes.push({
          method: verb === "all" ? "ANY" : verb,
          path: joinNestRoute(prefix, methodPath),
          line: lineOf(sourceFile, member.getStart(sourceFile)),
          source_kind: "nestjs_controller",
        });
      }
    }
  }

  function topVisit(node) {
    if (ts.isClassDeclaration(node)) visitClass(node);
    node.forEachChild(topVisit);
  }
  topVisit(sourceFile);
  return routes;
}

const KAFKAJS_EVENT_METHODS = { send: "PRODUCES_EVENT", subscribe: "CONSUMES_EVENT" };

// Raw kafkajs: `producer.send({ topic: "t", ... })` (produce) and `consumer.subscribe({ topic: "t" })`
// (consume). Gated on a `kafkajs` import; the distinctive `{ topic: ... }` object-literal arg keeps
// the generic `.send`/`.subscribe` method names from matching unrelated calls (e.g. RxJS subscribe).
function collectKafkaEvents(sourceFile) {
  if (!importsModule(sourceFile, "kafkajs")) return [];
  const events = [];
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const predicate = KAFKAJS_EVENT_METHODS[node.expression.name.text];
      if (predicate && node.arguments.length >= 1 && ts.isObjectLiteralExpression(node.arguments[0])) {
        const api = `kafkajs.${node.expression.name.text}`;
        const line = lineOf(sourceFile, node.expression.name.getStart(sourceFile));
        const pushTopic = (topicNode) => {
          const literal = stringLiteralValue(topicNode);
          events.push({
            predicate,
            broker: "kafka",
            api,
            line,
            channel: literal,
            channel_raw: literal == null ? rawNodeText(topicNode, sourceFile) : literal,
            reason: literal == null ? "non_literal_channel" : null,
          });
        };
        const arg = node.arguments[0];
        const topicNode = objectLiteralProperty(arg, "topic");
        const topicsNode = objectLiteralProperty(arg, "topics");
        if (topicNode) {
          pushTopic(topicNode);
        } else if (topicsNode && ts.isArrayLiteralExpression(topicsNode)) {
          for (const element of topicsNode.elements) pushTopic(element); // `{ topics: [...] }`
        } else {
          // shorthand `{ topic }` / `{ topics }` (or a non-array `topics`): the topic is a variable
          // we can't resolve at the call site -> recognize it but emit coverage, not a guess.
          const topicProperty = arg.properties.find(
            (property) => property.name && (propertyNameText(property.name) === "topic" || propertyNameText(property.name) === "topics")
          );
          if (topicProperty) {
            // record just the unresolved value expression (`topics: SOME_VAR` -> `SOME_VAR`,
            // shorthand `{ topic }` -> `topic`), consistent with channelFromArg.
            const valueNode = ts.isPropertyAssignment(topicProperty) ? topicProperty.initializer : topicProperty.name;
            events.push({
              predicate,
              broker: "kafka",
              api,
              line,
              channel: null,
              channel_raw: rawNodeText(valueNode, sourceFile),
              reason: "non_literal_channel",
            });
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return events;
}

// ---------------------------------------------------------------------------
// Async-lifecycle risk signal collector
// ---------------------------------------------------------------------------
// Signal 1: async_callback_in_iteration
//   Fires when .forEach() receives an async function/arrow as its first argument.
//   Conservative: only matches the identifier `forEach` on a call expression.
//
// Signal 2: unawaited_async_call
//   Fires when a call to a same-file async-declared function appears in a
//   position where the result is not awaited, not returned, not .then/.catch
//   chained, and not assigned to a variable.  Cross-file calls emit nothing
//   (documented limitation).
//
// Both signals carry { signal, qualname, callee, line } and are capped at 20
// per file (lowest line first). Per-subject retrieval bound of 3 is enforced
// at query time in _review_context_risk_signals.
//
// Limitation: collectAsyncFunctionNames only captures top-level async function
// declarations and top-level async arrow/function-expression variable bindings;
// async methods on classes and async functions nested inside other functions are
// not captured, so unawaited_async_call will not fire for those callees.

function collectAsyncFunctionNames(sourceFile) {
  // Return a Set of top-level async function names declared in this file.
  const names = new Set();
  for (const statement of sourceFile.statements) {
    if (
      ts.isFunctionDeclaration(statement) &&
      statement.name &&
      nodeHasModifier(statement, ts.SyntaxKind.AsyncKeyword)
    ) {
      names.add(statement.name.text);
    }
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        const init = declaration.initializer;
        if (
          init &&
          (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) &&
          nodeHasModifier(init, ts.SyntaxKind.AsyncKeyword) &&
          ts.isIdentifier(declaration.name)
        ) {
          names.add(declaration.name.text);
        }
      }
    }
  }
  return names;
}

function enclosingSymbolName(node, symbols) {
  // Return the name of the top-level symbol whose [pos, end] range contains node.pos.
  for (const sym of symbols) {
    if (node.pos >= sym.pos && node.end <= sym.end) return sym.name;
  }
  return null;
}

function isAsyncFunctionArg(node) {
  // True if node is an async function expression or arrow function.
  return (
    (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) &&
    nodeHasModifier(node, ts.SyntaxKind.AsyncKeyword)
  );
}

function isForEachCall(node) {
  // True if node is a call expression whose callee ends with `.forEach`.
  return (
    ts.isCallExpression(node) &&
    ts.isPropertyAccessExpression(node.expression) &&
    node.expression.name.text === "forEach"
  );
}

// Walk up through ParenthesizedExpression nodes and return {parent, child} where
// parent is the first non-paren ancestor and child is the immediate child of that
// ancestor (i.e. the outermost paren wrapping callNode, or callNode itself if
// there are no parens).  Used by all four suppression guards so that parenthesized
// forms — await (f()), return (f()), (f()).then(...), const x = (f()) — are
// treated identically to their unparenthesized counterparts.
function effectiveParentSkippingParens(callNode) {
  let child = callNode;
  let parent = callNode.parent;
  while (parent != null && ts.isParenthesizedExpression(parent)) {
    child = parent;
    parent = parent.parent;
  }
  return { parent, child };
}

function isAwaitedContext(callNode) {
  // True if the call expression (possibly wrapped in parens) is awaited.
  // Handles: await f(), await (f()), await ((f())).
  const { parent } = effectiveParentSkippingParens(callNode);
  return parent != null && parent.kind === ts.SyntaxKind.AwaitExpression;
}

function isReturnedContext(callNode) {
  // True if the call (possibly wrapped in parens) is directly inside a return statement.
  // Handles: return f(), return (f()), return ((f())).
  const { parent } = effectiveParentSkippingParens(callNode);
  return parent != null && parent.kind === ts.SyntaxKind.ReturnStatement;
}

function isThenCatchChained(callNode) {
  // True if the result of the call (possibly wrapped in parens) is immediately
  // .then()/.catch()/.finally() chained.
  // Handles: f().then(...), (f()).then(...).
  // The effective child (outermost paren or callNode itself) must be the
  // .expression side of the PropertyAccessExpression.
  const { parent, child } = effectiveParentSkippingParens(callNode);
  if (!parent) return false;
  if (
    ts.isPropertyAccessExpression(parent) &&
    parent.expression === child &&
    (parent.name.text === "then" || parent.name.text === "catch" || parent.name.text === "finally") &&
    parent.parent != null &&
    ts.isCallExpression(parent.parent) &&
    parent.parent.expression === parent
  ) {
    return true;
  }
  return false;
}

function isAssignedContext(callNode) {
  // True if the call (possibly wrapped in parens) appears on the RHS of a variable
  // declaration or an assignment expression (simple `=` or compound `+=`, `-=`, etc.).
  // ts.isBinaryExpression also covers comparisons (===, !==), logical (||, &&),
  // and arithmetic (+, -, *) — restrict to assignment operators only so that
  // `ready === saveRecord(id)` and `flag || saveRecord(id)` still emit signals.
  // ASSIGNMENT_OPERATORS covers SyntaxKind.EqualsToken through the full
  // compound-assignment range (FirstAssignment..LastAssignment).
  // Handles: const x = (f()), x = (f()), x += (f()).
  const { parent, child } = effectiveParentSkippingParens(callNode);
  if (!parent) return false;
  // `const x = callNode` or `let x = callNode`
  if (ts.isVariableDeclaration(parent) && parent.initializer === child) return true;
  // `x = callNode`, `x += callNode`, etc. — assignment operators only
  if (
    ts.isBinaryExpression(parent) &&
    parent.right === child &&
    ASSIGNMENT_OPERATORS.has(parent.operatorToken.kind)
  ) return true;
  return false;
}

function isInsidePromiseAll(callNode) {
  // True if the call is nested inside a Promise.all/allSettled/race/any invocation.
  // Walk parent chain; cross array literals, arrow functions, and array-method
  // call expressions (map/flatMap) until we either find Promise.* or hit a hard
  // boundary (any other call expression, block, or function boundary).
  // Start from the effective parent (skip any wrapping ParenthesizedExpression nodes)
  // so that ids.map(id => (processItem(id))) is treated like ids.map(id => processItem(id)).
  const { child: _child, parent: startParent } = effectiveParentSkippingParens(callNode);
  let node = startParent;
  while (node) {
    if (ts.isCallExpression(node)) {
      const expr = node.expression;
      if (
        ts.isPropertyAccessExpression(expr) &&
        ts.isIdentifier(expr.expression) &&
        expr.expression.text === "Promise" &&
        (expr.name.text === "all" || expr.name.text === "allSettled" || expr.name.text === "race" || expr.name.text === "any")
      ) {
        return true;
      }
      // Allow map/flatMap calls through (common pattern: Promise.all(arr.map(...)))
      if (
        ts.isPropertyAccessExpression(expr) &&
        (expr.name.text === "map" || expr.name.text === "flatMap")
      ) {
        node = node.parent;
        continue;
      }
      // Any other call: stop
      return false;
    }
    if (
      ts.isArrayLiteralExpression(node) ||
      ts.isArrowFunction(node) ||
      ts.isFunctionExpression(node) ||
      ts.isParenthesizedExpression(node)  // parens are transparent anywhere in the walk
    ) {
      node = node.parent;
      continue;
    }
    // Anything else (block, statement, etc.) is a hard boundary
    break;
  }
  return false;
}

function applySignalCap(rawSignals) {
  // File-level cap: emit at most 20 signals per file (all families combined).
  // Per-subject retrieval bound (3 signals per enclosing symbol, relevance-first)
  // is enforced at query time in _review_context_risk_signals.
  rawSignals.sort((a, b) => a.line - b.line);
  return rawSignals.slice(0, 20);
}

function collectAsyncLifecycleSignals(sourceFile, symbols) {
  const asyncNames = collectAsyncFunctionNames(sourceFile);
  const rawSignals = [];

  function visit(node) {
    // Signal 1: forEach(async callback)
    if (isForEachCall(node) && node.arguments.length > 0 && isAsyncFunctionArg(node.arguments[0])) {
      const line = lineOf(sourceFile, node.getStart(sourceFile));
      const qualname = enclosingSymbolName(node, symbols) ?? "<module>";
      rawSignals.push({
        signal: "async_callback_in_iteration",
        qualname,
        callee: "forEach",
        line,
      });
    }

    // Signal 2: unawaited call to same-file async fn
    if (ts.isCallExpression(node) && !isForEachCall(node)) {  // forEach handled above
      const { parent: effectiveParent } = effectiveParentSkippingParens(node);
      if (
        !isAwaitedContext(node) &&
        !isReturnedContext(node) &&
        !isThenCatchChained(node) &&
        !isAssignedContext(node) &&
        !isInsidePromiseAll(node) &&
        !(effectiveParent != null && ts.isVoidExpression(effectiveParent))  // void (fn()) is intentional discard
      ) {
        const name = callName(node.expression, sourceFile);
        // Only the leaf name for same-file check (no dots = top-level call)
        const leafName = name && !name.includes(".") ? name : null;
        if (leafName && asyncNames.has(leafName)) {
          const line = lineOf(sourceFile, node.getStart(sourceFile));
          const qualname = enclosingSymbolName(node, symbols) ?? "<module>";
          rawSignals.push({
            signal: "unawaited_async_call",
            qualname,
            callee: leafName,
            line,
          });
        }
      }
    }

    ts.forEachChild(node, visit);
  }
  visit(sourceFile);

  return applySignalCap(rawSignals);
}

const output = Object.create(null);
for (const relativePath of files) {
  const absolutePath = path.join(repoRoot, relativePath);
  const sourceText = fs.readFileSync(absolutePath, "utf8");
  const sourceFile = ts.createSourceFile(absolutePath, sourceText, ts.ScriptTarget.Latest, true, scriptKind(relativePath));
  const symbols = collectSymbols(sourceFile);
  const axiosLocals = collectAxiosLocals(sourceFile);
  const literalBindings = collectTopLevelLiteralBindings(sourceFile);
  const serverRoutes = [...collectServerRoutes(sourceFile), ...collectNestRoutes(sourceFile)];
  output[relativePath] = {
    parse_diagnostics: sourceFile.parseDiagnostics.map((diagnostic) => ({
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
      line: diagnostic.start == null ? 1 : lineOf(sourceFile, diagnostic.start),
    })),
    imports: collectImports(sourceFile),
    server_routes: serverRoutes,
    express_routes: serverRoutes,
    client_endpoint_calls: collectClientEndpointCalls(sourceFile),
    message_events: [...collectMessageEvents(sourceFile), ...collectKafkaEvents(sourceFile)],
    module_clients: collectModuleClients(sourceFile, axiosLocals, literalBindings),
    literal_exports: collectLiteralExports(sourceFile),
    literal_aliases: collectTopLevelLiteralAliases(sourceFile),
    symbols: symbols.map(({ pos, end, ...symbol }) => symbol),
    calls: symbols.flatMap((symbol) =>
      collectCallsForSymbol(sourceFile, symbol).map((call) => ({
        caller: symbol.name,
        name: call.name,
        line: call.line,
      }))
    ),
    async_lifecycle_signals: collectAsyncLifecycleSignals(sourceFile, symbols),
  };
}

process.stdout.write(JSON.stringify(output));
